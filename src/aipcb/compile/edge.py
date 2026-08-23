# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Card-edge connectors: the geometry three other modules need (M11b).

A card-edge footprint is unlike every other footprint this tool places, in one
way that matters: it carries `Edge.Cuts` graphics of its own. Those graphics are
the board outline the connector requires -- the tongue the fingers sit on, and the
keying notch that stops the card going in backwards.

**aipcb treats that geometry as a specification, not as a contribution.** The
footprint's `Edge.Cuts` is stripped when the board is written, and the design's own
``board:`` block has to reproduce it; :mod:`aipcb.checks.edge` is what compares the
two. The alternative -- letting the footprint draw part of the outline -- gives a
board two authors for one edge, and the two disagree the moment anything moves.
It would also be invisible to this project's own router, whose free space comes
from the top-level `Edge.Cuts` graphics and not from inside a footprint.

Everything here is pure geometry over a resolved footprint and a placement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from aipcb.compile.frame import BoardFrame
from aipcb.compile.geometry import rotate_kicad
from aipcb.compile.place import BoardPlacement
from aipcb.kicad.footprints import (
    Footprint,
    FootprintNotFound,
    resolve_footprint,
)
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import Netlist

__all__ = [
    "AC_COUPLING_ROLE",
    "DEFAULT_FINGER_KEEPOUT_MM",
    "EDGE_ROLE",
    "EdgeConnector",
    "EdgeFinger",
    "edge_connectors",
    "edge_refdes",
    "footprint_edge_paths",
    "strip_edge_graphics",
]

Point = tuple[float, float]
Polyline = tuple[Point, ...]

#: The component role that turns the integration behaviours on.
EDGE_ROLE = "edge_connector"
#: The role that marks a series capacitor as part of a high-speed pair (M11c).
AC_COUPLING_ROLE = "ac_coupling"

#: How far from the fingers a pour is kept, when the placement does not say. Half a
#: millimetre: enough that plating and the pour do not meet, small enough that the
#: ground under the finger field is still a reference for the pairs entering it.
DEFAULT_FINGER_KEEPOUT_MM = 0.5

#: Points per full circle when an ``fp_arc`` is sampled. Two degrees, which puts
#: the chord error on a 1 mm radius under a micron.
_ARC_STEPS = 180


@dataclass(frozen=True, slots=True)
class EdgeFinger:
    """One gold finger, in KiCad board coordinates."""

    pad: str
    """The pad number as the footprint prints it, e.g. ``A16``."""
    instance: str
    """The per-pad-instance key, ``J1.A16`` or ``J1.A16#2``."""
    layer: str
    polygon: Polyline
    centre: Point
    inner: Point
    """The middle of the finger's inboard end -- where a track should meet it."""
    outward: Point
    """Unit vector from the board's interior toward the card edge."""

    @property
    def length(self) -> float:
        return math.dist(self.inner, self.centre) * 2


@dataclass(frozen=True, slots=True)
class EdgeConnector:
    """A placed card-edge connector and everything derived from it."""

    refdes: str
    lib_id: str
    position: Point
    rotation: float
    paths: tuple[Polyline, ...]
    """The footprint's own ``Edge.Cuts`` geometry, in KiCad board coordinates."""
    fingers: tuple[EdgeFinger, ...]
    keepout_mm: float

    @property
    def finger_box(self) -> tuple[float, float, float, float]:
        """Bounding box of the finger field, in KiCad coordinates."""
        xs = [x for finger in self.fingers for x, _ in finger.polygon]
        ys = [y for finger in self.fingers for _, y in finger.polygon]
        if not xs:
            return (self.position[0], self.position[1], self.position[0], self.position[1])
        return (min(xs), min(ys), max(xs), max(ys))

    def keepout_polygon(self) -> Polyline:
        """The finger field, grown by the pour keepout. KiCad coordinates."""
        x1, y1, x2, y2 = self.finger_box
        pad = self.keepout_mm
        return (
            (round(x1 - pad, 4), round(y1 - pad, 4)),
            (round(x2 + pad, 4), round(y1 - pad, 4)),
            (round(x2 + pad, 4), round(y2 + pad, 4)),
            (round(x1 - pad, 4), round(y2 + pad, 4)),
        )


def edge_refdes(netlist: Netlist) -> tuple[str, ...]:
    """Reference designators of every component with ``role: edge_connector``."""
    return tuple(
        component.refdes for component in netlist.components_with_role(EDGE_ROLE)
    )


# ---------------------------------------------------------------------------
# the footprint's own edge geometry
# ---------------------------------------------------------------------------


def footprint_edge_paths(node: SNode) -> tuple[Polyline, ...]:
    """Every ``Edge.Cuts`` primitive in a footprint, as polylines in local space.

    Chains are *not* reconstructed. A gap between two primitives is a gap the
    designer has to close with their own outline, and joining them here would
    invent an edge the footprint does not draw.
    """
    paths: list[Polyline] = []
    for line in node.children("fp_line"):
        if line.get("layer") != "Edge.Cuts":
            continue
        start, end = _xy(line, "start"), _xy(line, "end")
        if start is not None and end is not None:
            paths.append((start, end))
    for arc in node.children("fp_arc"):
        if arc.get("layer") != "Edge.Cuts":
            continue
        start, mid, end = _xy(arc, "start"), _xy(arc, "mid"), _xy(arc, "end")
        if start is not None and mid is not None and end is not None:
            paths.append(_sample_arc(start, mid, end))
    for rect in node.children("fp_rect"):
        if rect.get("layer") != "Edge.Cuts":
            continue
        start, end = _xy(rect, "start"), _xy(rect, "end")
        if start is not None and end is not None:
            (x1, y1), (x2, y2) = start, end
            paths.append(((x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)))
    for poly in node.children("fp_poly"):
        if poly.get("layer") != "Edge.Cuts":
            continue
        pts = poly.child("pts")
        if pts is None:
            continue
        ring = tuple(
            (float(xy.value(0) or 0.0), float(xy.value(1) or 0.0))
            for xy in pts.children("xy")
        )
        if len(ring) >= 3:
            paths.append((*ring, ring[0]))
    return tuple(paths)


def strip_edge_graphics(node: SNode) -> int:
    """Remove a footprint's ``Edge.Cuts`` graphics. Returns how many went.

    Called for ``role: edge_connector`` footprints only, and the reason is in this
    module's docstring: the outline has one author, and it is the ``board:`` block.
    """
    removed = 0
    for item in list(node.items):
        if not isinstance(item, SNode):
            continue
        if item.name not in ("fp_line", "fp_arc", "fp_rect", "fp_poly", "fp_circle"):
            continue
        if item.get("layer") != "Edge.Cuts":
            continue
        node.items.remove(item)
        removed += 1
    return removed


def _xy(node: SNode, name: str) -> Point | None:
    child = node.child(name)
    if child is None:
        return None
    return (float(child.value(0) or 0.0), float(child.value(1) or 0.0))


def _sample_arc(start: Point, mid: Point, end: Point) -> Polyline:
    """An arc through three points, as a polyline including both ends."""
    centre = _circumcentre(start, mid, end)
    if centre is None:
        return (start, mid, end)
    cx, cy = centre
    radius = math.dist(centre, start)
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    a1 = math.atan2(mid[1] - cy, mid[0] - cx)
    a2 = math.atan2(end[1] - cy, end[0] - cx)
    # Walk start -> mid -> end the short way round each hop, which is what an arc
    # through a midpoint means.
    sweep = _wrap(a1 - a0) + _wrap(a2 - a1)
    steps = max(2, int(abs(sweep) / (2 * math.pi) * _ARC_STEPS) + 1)
    return tuple(
        (
            round(cx + radius * math.cos(a0 + sweep * i / steps), 6),
            round(cy + radius * math.sin(a0 + sweep * i / steps), 6),
        )
        for i in range(steps + 1)
    )


def _wrap(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _circumcentre(a: Point, b: Point, c: Point) -> Point | None:
    d = 2 * (
        a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1])
    )
    if abs(d) < 1e-12:
        return None
    aa = a[0] ** 2 + a[1] ** 2
    bb = b[0] ** 2 + b[1] ** 2
    cc = c[0] ** 2 + c[1] ** 2
    return (
        (aa * (b[1] - c[1]) + bb * (c[1] - a[1]) + cc * (a[1] - b[1])) / d,
        (aa * (c[0] - b[0]) + bb * (a[0] - c[0]) + cc * (b[0] - a[0])) / d,
    )


# ---------------------------------------------------------------------------
# resolving a placed connector
# ---------------------------------------------------------------------------


def edge_connectors(
    netlist: Netlist,
    placement: BoardPlacement,
    frame: BoardFrame | None = None,
) -> list[EdgeConnector]:
    """Every placed edge connector, with its geometry in KiCad coordinates.

    Components whose footprint cannot be resolved are skipped silently: the
    missing-footprint diagnostic that ``check_kicad_bindings`` already produces is
    the useful one, and a second complaint about the same absence is noise.
    """
    del frame  # kept for symmetry with the other geometry helpers
    out: list[EdgeConnector] = []
    for component in netlist.components_with_role(EDGE_ROLE):
        placed = placement.positions.get(component.refdes)
        if placed is None or component.part is None:
            continue
        try:
            footprint = resolve_footprint(component.part.footprint)
        except FootprintNotFound:
            continue
        out.append(
            _resolve(
                component.refdes,
                component.part.footprint,
                footprint,
                (placed.x, placed.y),
                placed.rotation,
                _keepout_for(netlist, component.refdes),
            )
        )
    return out


def _keepout_for(netlist: Netlist, refdes: str) -> float:
    entry = netlist.placement.get(refdes)
    if entry is not None and entry.pour_keepout_mm is not None:
        return entry.pour_keepout_mm
    return DEFAULT_FINGER_KEEPOUT_MM


def _resolve(
    refdes: str,
    lib_id: str,
    footprint: Footprint,
    position: Point,
    rotation: float,
    keepout: float,
) -> EdgeConnector:
    paths = tuple(
        tuple(_to_board(point, position, rotation) for point in path)
        for path in footprint_edge_paths(footprint.node)
    )
    fingers = _fingers(refdes, footprint.node, position, rotation, paths)
    return EdgeConnector(
        refdes=refdes,
        lib_id=lib_id,
        position=position,
        rotation=rotation,
        paths=paths,
        fingers=fingers,
        keepout_mm=keepout,
    )


def _to_board(point: Point, position: Point, rotation: float) -> Point:
    x, y = rotate_kicad(point, rotation)
    return (round(position[0] + x, 6), round(position[1] + y, 6))


def _fingers(
    refdes: str,
    node: SNode,
    position: Point,
    rotation: float,
    paths: tuple[Polyline, ...],
) -> tuple[EdgeFinger, ...]:
    """Every pad, with the end that faces the card edge worked out.

    The outward direction is decided per pad, from the pad's own two ends: whichever
    is nearer the footprint's declared edge geometry is the outboard one. Deciding
    it per pad rather than per footprint is what makes this work on a connector
    whose two rows face opposite ways.
    """
    seen: dict[str, int] = {}
    raw: list[tuple[str, str, str, Polyline, Point, Point, Point]] = []
    for pad in node.children("pad"):
        number = pad.value(0)
        at = pad.child("at")
        size = pad.child("size")
        if number is None or at is None or size is None:
            continue
        count = seen.get(number, 0) + 1
        seen[number] = count
        instance = f"{refdes}.{number}" if count == 1 else f"{refdes}.{number}#{count}"

        px, py = float(at.value(0) or 0.0), float(at.value(1) or 0.0)
        width, height = float(size.value(0) or 0.0), float(size.value(1) or 0.0)
        local = (
            (px - width / 2, py - height / 2),
            (px + width / 2, py - height / 2),
            (px + width / 2, py + height / 2),
            (px - width / 2, py + height / 2),
        )
        polygon = tuple(_to_board(p, position, rotation) for p in local)
        centre = _to_board((px, py), position, rotation)

        # The finger runs along its long axis; its two ends are the midpoints of
        # the short sides.
        if height >= width:
            ends = ((px, py - height / 2), (px, py + height / 2))
        else:
            ends = ((px - width / 2, py), (px + width / 2, py))
        board_ends = tuple(_to_board(p, position, rotation) for p in ends)
        outboard, inboard = _order_by_edge(board_ends, paths)
        span = math.dist(outboard, inboard) or 1.0
        outward = (
            round((outboard[0] - inboard[0]) / span, 6),
            round((outboard[1] - inboard[1]) / span, 6),
        )
        layers = pad.child("layers")
        layer = "F.Cu"
        if layers is not None:
            copper = [
                str(a.value) for a in layers.atoms() if str(a.value).endswith(".Cu")
            ]
            layer = copper[0] if copper else "F.Cu"
        raw.append(
            (str(number), instance, layer, polygon, centre, inboard, outward)
        )

    # One card edge, one direction. Deciding it per pad is right for a connector
    # whose two rows face opposite ways, and wrong for the pad beside a keying
    # notch, whose nearest piece of declared edge is the notch rather than the tip.
    # A vote per layer gets both cases: the notch-adjacent pads are outvoted by the
    # thirty-four that agree with each other.
    votes: dict[str, dict[Point, int]] = {}
    for _, _, layer, _, _, _, outward in raw:
        votes.setdefault(layer, {})
        votes[layer][outward] = votes[layer].get(outward, 0) + 1
    agreed = {
        layer: max(sorted(counts), key=lambda d: counts[d])
        for layer, counts in votes.items()
    }

    out: list[EdgeFinger] = []
    for number, instance, layer, polygon, centre, inboard, outward in raw:
        direction = agreed.get(layer, outward)
        span = math.dist(centre, inboard)
        inner = (
            round(centre[0] - direction[0] * span, 6),
            round(centre[1] - direction[1] * span, 6),
        )
        out.append(
            EdgeFinger(
                pad=number,
                instance=instance,
                layer=layer,
                polygon=polygon,
                centre=centre,
                inner=inner,
                outward=direction,
            )
        )
    return tuple(out)


def _order_by_edge(
    ends: tuple[Point, ...], paths: tuple[Polyline, ...]
) -> tuple[Point, Point]:
    """``(outboard, inboard)``: the end nearer the declared edge comes first."""
    if len(ends) != 2:
        return (ends[0], ends[0])
    if not paths:
        return (ends[0], ends[1])
    first, second = (_distance_to_paths(end, paths) for end in ends)
    return (ends[0], ends[1]) if first <= second else (ends[1], ends[0])


def _distance_to_paths(point: Point, paths: tuple[Polyline, ...]) -> float:
    best = float("inf")
    for path in paths:
        for a, b in pairwise(path):
            best = min(best, _distance_to_segment(point, a, b))
    return best


def _distance_to_segment(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span <= 0:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / span))
    return math.dist(point, (ax + t * dx, ay + t * dy))

"""The property a finished board must have: two nets' copper never touches.

Everything else in :mod:`aipcb.route` is *constructive* -- a route is tightened
inside free space that already has the other nets' copper removed from it, so a
legal result is supposed to fall out of the construction rather than be checked
afterwards. That is the right design and it is why there was no check here before
M13.

The trouble with a construction argument is that it holds only while every input
to the construction is what it claims to be. M11 found a repair pass that routed
`PRSNT` across two `REFCLK` tracks and could not explain it, because the copper it
crossed *was* in the list of finished obstacles. M13 found the missing step: the
list becomes a dict keyed by obstacle name on the way into the free-space
calculation, and two pieces of copper on two layers shared a name, so one of them
was overwritten and never reached the triangulation at all. The construction was
sound; one of its inputs had quietly lost four polygons.

No amount of care in the constructive path catches that, because the failure is
*upstream* of it. What catches it is asking the finished board the blunt question
this module asks. It is cheap -- an R-tree query per piece of copper, single-digit
milliseconds on the densest bundled example -- and it is the check that turns a
class of silent wrong answers into a loud one.

The check is deliberately about *intersection*, not clearance. KiCad's DRC already
measures clearance, against the filled board, with the fabricator's rules; running
a second, worse copy of that here would produce a different number from the one
that matters. Overlap needs no rules to be wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from aipcb.diagnostics import Report
from aipcb.route.stretch import RoutedConnection

__all__ = ["Crossing", "check_no_crossings", "crossing_nets"]

Point = tuple[float, float]

#: How many segments approximate a via's barrel. Eight is what the obstacle
#: extractor uses for a pad, and the same reasoning applies: the error is far under
#: a fabrication tolerance and the polygon stays cheap to intersect.
_VIA_SEGMENTS = 8

#: Overlap smaller than this is not two nets crossing, it is two polygons that
#: meet at a shared boundary point and disagree in the last bit of a double. A
#: square micron is a millionth of the area of the smallest real crossing this has
#: ever produced (the `PRSNT`/`REFCLK` one is 0.058 mm^2, fifty thousand times
#: larger), so the threshold separates the two by a wide margin rather than by a
#: fudge.
_NEGLIGIBLE_MM2 = 1e-6


@dataclass(frozen=True, slots=True)
class Crossing:
    """Two nets' copper occupying the same place on the same layer."""

    first: str
    second: str
    layer: str
    area_mm2: float
    at: Point

    def describe(self) -> str:
        return (
            f"{self.first} and {self.second} overlap on {self.layer} by "
            f"{self.area_mm2:.4f} mm^2 at ({self.at[0]:.3f}, {self.at[1]:.3f})"
        )


def _via_ring(centre: Point, diameter: float) -> list[Point]:
    radius = diameter / 2
    return [
        (
            centre[0] + radius * math.cos(math.tau * i / _VIA_SEGMENTS),
            centre[1] + radius * math.sin(math.tau * i / _VIA_SEGMENTS),
        )
        for i in range(_VIA_SEGMENTS)
    ]


def crossing_nets(
    connections: list[RoutedConnection],
    *,
    barrel_layers: dict[str, tuple[str, ...]] | None = None,
) -> list[Crossing]:
    """Every place two different nets' finished copper overlaps, worst first.

    ``barrel_layers`` maps a via's ``(from_layer, to_layer)`` span, joined by a
    slash, to the copper layers its barrel actually passes through. Without it a
    via is tested against every layer that carries copper, which is the
    conservative reading and the right default: a through via is the common case
    and it does pierce every layer.
    """
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon

    pieces: list[tuple[str, str, Any]] = []
    for connection in connections:
        for leg in connection.legs:
            if len(leg.points) < 2:
                continue
            body = LineString(leg.points).buffer(
                leg.width / 2, cap_style="flat", join_style="mitre"
            )
            if not body.is_empty:
                pieces.append((leg.net, leg.layer, body))
        for via in connection.vias:
            span = f"{via.from_layer}/{via.to_layer}"
            layers = (barrel_layers or {}).get(span)
            ring = Polygon(_via_ring(via.point, via.diameter))
            for layer in layers or ("*",):
                pieces.append((via.net, layer, ring))

    if len(pieces) < 2:
        return []

    tree = STRtree([geometry for _, _, geometry in pieces])
    found: dict[tuple[str, str, str], Crossing] = {}
    for index, (net, layer, geometry) in enumerate(pieces):
        for other in tree.query(geometry):
            other = int(other)
            if other <= index:
                continue
            their_net, their_layer, theirs = pieces[other]
            if their_net == net:
                continue
            if layer != their_layer and "*" not in (layer, their_layer):
                continue
            overlap = geometry.intersection(theirs)
            if overlap.is_empty or overlap.area <= _NEGLIGIBLE_MM2:
                continue
            where = layer if layer != "*" else their_layer
            first, second = sorted((net, their_net))
            key = (first, second, where)
            centre = overlap.centroid
            crossing = Crossing(
                first=first,
                second=second,
                layer=where,
                area_mm2=overlap.area,
                at=(round(centre.x, 4), round(centre.y, 4)),
            )
            previous = found.get(key)
            if previous is None or crossing.area_mm2 > previous.area_mm2:
                found[key] = crossing
    return sorted(found.values(), key=lambda c: (-c.area_mm2, c.first, c.second))


def check_no_crossings(
    connections: list[RoutedConnection],
    report: Report,
    *,
    barrel_layers: dict[str, tuple[str, ...]] | None = None,
) -> list[Crossing]:
    """Run the invariant and report anything it finds as an error.

    An error rather than a warning, and unconditionally rather than behind a flag.
    Copper of two nets in one place is a short circuit; there is no board on which
    that is a judgement call, and a router that has just produced one should not
    be the thing that decides how loudly to say so.
    """
    crossings = crossing_nets(connections, barrel_layers=barrel_layers)
    for crossing in crossings:
        report.error(
            "route-nets-cross",
            crossing.describe(),
            hint="two nets' copper cannot share a place; this is a router defect "
            "rather than a design one, and the board it produced is a short "
            "circuit",
            net=crossing.first,
        )
    return crossings

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
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

M16b added a second question, about one net rather than two. :func:`crossing_nets`
skips same-net pairs and has to -- two connections of one net are *supposed* to
meet -- so nothing anywhere asked whether a single route crosses *itself*. One
funnel output is simple by construction, because the shortest path in a simple
polygon is simple; a route that changes layer is a concatenation of several funnel
outputs and that argument does not reach the join. The gEDA toporouter's tightener
produced exactly this class of geometry -- its arc-loop checks existed for no other
reason, and its author spent over a hundred hours there. :func:`self_crossings` is
the same blunt question asked of one route: is every leg a simple polyline, and do
two legs of one connection on one layer meet anywhere but at a shared end.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from aipcb.diagnostics import Report
from aipcb.route.stretch import RoutedConnection

__all__ = [
    "Crossing",
    "SelfCrossing",
    "check_no_crossings",
    "check_no_self_crossings",
    "crossing_nets",
    "self_crossings",
]

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

#: How close to a leg's own endpoint a shared point has to be before it counts as
#: the join a via makes rather than geometry doubling back. A micron: three orders
#: below the coordinate quantum the triangulation snaps to, so it absorbs the last
#: bits of a double and nothing else.
_JOIN_TOLERANCE_MM = 1e-6


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


# ---------------------------------------------------------------------------
# one route against itself (M16b, postmortem exposure E2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelfCrossing:
    """One connection's copper meeting its own, somewhere it should not."""

    net: str
    connection: str
    layer: str
    kind: str
    """``leg-not-simple`` -- one tightened leg touches or crosses itself.
    ``legs-meet`` -- two legs of the connection on one layer meet away from a join.
    """
    at: Point

    def describe(self) -> str:
        what = (
            "crosses itself"
            if self.kind == "leg-not-simple"
            else "has two legs that meet away from their shared end"
        )
        return (
            f"{self.net} {self.connection} {what} on {self.layer} at "
            f"({self.at[0]:.3f}, {self.at[1]:.3f})"
        )


def _self_touch(points: list[Point], line: Any) -> Point:
    """Where a non-simple polyline meets itself, for the diagnostic to point at.

    Two cases and both are wanted. A polyline that returns to a vertex it already
    visited names that vertex; one whose *segments* cross in their interiors touches
    at a point that is in none of its coordinates, and noding the line introduces
    exactly that point. Taking the smallest of the new coordinates keeps the answer
    a function of the geometry rather than of Shapely's traversal order.
    """
    from shapely.ops import unary_union

    seen: set[Point] = set()
    for point in points:
        if point in seen:
            return (round(point[0], 4), round(point[1], 4))
        seen.add(point)

    noded = unary_union(line)
    introduced = {
        (round(x, 4), round(y, 4))
        for piece in getattr(noded, "geoms", (noded,))
        for x, y in piece.coords
    } - {(round(x, 4), round(y, 4)) for x, y in points}
    if introduced:
        return min(introduced)
    return (round(points[0][0], 4), round(points[0][1], 4))  # pragma: no cover


def _connection_key(connection: RoutedConnection) -> str:
    return f"{connection.start}>{connection.end}" if connection.start else "route"


def self_crossings(connections: list[RoutedConnection]) -> list[SelfCrossing]:
    """Every place one connection's own copper meets itself, worst first.

    Two questions, both on centre-lines rather than on buffered copper, and that is
    deliberate. Two legs that join at a via *do* share copper around the joint, and
    a buffered test would have to subtract a disc of the right radius at every
    junction to avoid calling every normal route a defect -- a threshold to tune,
    which is the shape of check this project keeps refusing to write. A centre-line
    that crosses another centre-line has no legitimate reading.

    * **Is each leg simple?** ``LineString.is_simple`` is false exactly when a
      polyline touches or crosses itself. A single funnel output cannot fail this;
      a leg assembled from more than one -- a repair, a pair half fed back through
      its partner's geometry -- is not covered by that argument.
    * **Do two legs on one layer meet?** Legs of one connection normally live on
      different layers with a via between them, so any shared point on *one* layer
      is either the join where a via lands, which is allowed and named by the legs'
      own ``start``/``end``, or geometry doubling back on itself, which is not.
    """
    from shapely.geometry import LineString
    from shapely.geometry import Point as ShapelyPoint

    found: list[SelfCrossing] = []
    for connection in connections:
        key = _connection_key(connection)
        lines: list[tuple[int, Any]] = []
        for index, leg in enumerate(connection.legs):
            if len(leg.points) < 2:
                continue
            line = LineString(leg.points)
            lines.append((index, line))
            if line.is_simple:
                continue
            found.append(
                SelfCrossing(
                    net=leg.net,
                    connection=key,
                    layer=leg.layer,
                    kind="leg-not-simple",
                    at=_self_touch(leg.points, line),
                )
            )

        for first in range(len(lines)):
            index, line = lines[first]
            leg = connection.legs[index]
            for second in range(first + 1, len(lines)):
                other_index, other = lines[second]
                other_leg = connection.legs[other_index]
                if other_leg.layer != leg.layer:
                    continue
                shared = line.intersection(other)
                if shared.is_empty:
                    continue
                # A via lands where one leg ends and the next begins. Those points
                # are named, not guessed: they are the legs' own endpoints.
                joins = [
                    ShapelyPoint(p)
                    for p in (
                        leg.points[0],
                        leg.points[-1],
                        other_leg.points[0],
                        other_leg.points[-1],
                    )
                ]
                for join in joins:
                    shared = shared.difference(join.buffer(_JOIN_TOLERANCE_MM))
                    if shared.is_empty:
                        break
                if shared.is_empty:
                    continue
                centre = shared.centroid
                found.append(
                    SelfCrossing(
                        net=leg.net,
                        connection=key,
                        layer=leg.layer,
                        kind="legs-meet",
                        at=(round(centre.x, 4), round(centre.y, 4)),
                    )
                )
    return sorted(found, key=lambda c: (c.net, c.connection, c.layer, c.kind, c.at))


def check_no_self_crossings(
    connections: list[RoutedConnection], report: Report
) -> list[SelfCrossing]:
    """Run the self-crossing invariant and report what it finds.

    **Two severities, because the two findings are not the same kind of thing.**

    A leg that is not simple is an *error*, for the same reason
    :func:`check_no_crossings` reports one: a tightened polyline crossing itself is
    not a design judgement anybody gets to make, it is a defect in the thing that
    produced it, and no board should ever carry one.

    Two legs of a connection meeting on one layer is a *warning*. It is copper laid
    twice rather than copper in the wrong place -- electrically the net is still the
    net, and KiCad's DRC has nothing to say about it -- so the honest report is
    waste, not a short circuit. It is not hypothetical: on `examples/pcie-sata` at
    M16, GND's `U1.17>U1.49` travels four millimetres east, hops to B.Cu for half a
    millimetre, hops straight back to F.Cu, and retraces its own path home. Roughly
    eight millimetres of copper and two vias buy nothing. That is a search and
    cost-model defect, not a geometry one, and fixing it is what the roadmap's
    post-convergence detour pass is for -- measured against the M16c baseline, since
    it is exactly the kind of change that trades runtime for quality. Until then it
    is *visible*, which it was not before, and `tests/test_routing.py` pins it so
    the day it changes is a day somebody notices.
    """
    crossings = self_crossings(connections)
    for crossing in crossings:
        if crossing.kind == "leg-not-simple":
            report.error(
                "route-crosses-itself",
                crossing.describe(),
                hint="a tightened leg crossing itself is a router defect rather "
                "than a design one",
                net=crossing.net,
            )
        else:
            report.warning(
                "route-doubles-back",
                crossing.describe(),
                hint="the connection lays copper twice along the same corridor; "
                "the net is still correct, the copper is wasted",
                net=crossing.net,
            )
    return crossings

"""The stretcher: topology in, DRC-clean geometry out.

This is the pure function ADR 0006 promised. Give it a sketch and a placed board
and it returns track segments; it holds no state, touches no files, and returns the
same answer every time.

The pipeline, per route:

1. build the obstacle set for this net and layer, each hull inflated by the
   clearance the net class asks for;
2. triangulate what is left -- the region the wire's centre-line may occupy;
3. turn the sketch into a walk through triangles, forced through a point on the
   named side of each obstacle it must pass;
4. read off the crossing sequence and reduce it, giving the homotopy class;
5. tighten the resulting sleeve with the funnel algorithm.

Because the obstacles were inflated first, step 5's shortest path is a legal path:
clearance is satisfied by construction rather than checked and patched afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from itertools import pairwise

from aipcb.route.funnel import Portal, orient_portals, tighten
from aipcb.route.model import Pass, RouteTopology, ViaHop
from aipcb.route.obstacles import (
    Obstacle,
    RoutingEnvironment,
    extract_obstacles,
    inflate,
)
from aipcb.route.triangulate import (
    FreeSpaceError,
    Triangulation,
    build_triangulation,
    reduce_crossings,
)

__all__ = [
    "RouteRules",
    "StretchError",
    "StretchResult",
    "environment_for",
    "side_gate",
    "side_point",
    "stretch_route",
]

Point = tuple[float, float]

#: How far beyond an inflated hull a side point is placed, in millimetres. Small
#: enough not to distort the topology, large enough to land clear of the boundary.
SIDE_GAP = 0.05


class StretchError(ValueError):
    """A sketch could not be turned into geometry, with the reason why."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True, slots=True)
class RouteRules:
    """The electrical rules a route must satisfy."""

    track_width: float = 0.25
    clearance: float = 0.2
    via_diameter: float = 0.6
    via_drill: float = 0.3


@dataclass(slots=True)
class StretchResult:
    """One tightened route."""

    net: str
    layer: str
    points: list[Point] = field(default_factory=list)
    width: float = 0.25
    crossings: int = 0
    """How many triangulation diagonals the reduced homotopy class crosses."""

    @property
    def length(self) -> float:
        return sum(
            math.dist(a, b) for a, b in pairwise(self.points)
        )

    @property
    def segments(self) -> list[tuple[Point, Point]]:
        return list(pairwise(self.points))


def environment_for(board: object) -> RoutingEnvironment:
    """Read a board's physical obstacles."""
    from aipcb.kicad.sexpr import SNode

    assert isinstance(board, SNode)
    return extract_obstacles(board)


# ---------------------------------------------------------------------------
# side points
# ---------------------------------------------------------------------------


def side_point(
    obstacle: Obstacle, direction: Point, side: str, gap: float = SIDE_GAP
) -> Point:
    """A point just clear of ``obstacle`` on the named side of ``direction``.

    Board coordinates have Y pointing down, so "left of travel" is the direction
    ``(dy, -dx)`` -- rotating the other way would put every route on the wrong side
    of every obstacle, silently, while still producing a legal-looking board.
    """
    unit = _unit(direction)
    normal = (unit[1], -unit[0]) if side == "left" else (-unit[1], unit[0])
    centre = obstacle.centroid()
    offset = _reach(obstacle, centre, normal) + gap
    return (centre[0] + normal[0] * offset, centre[1] + normal[1] * offset)


def side_gate(
    obstacle: Obstacle, direction: Point, side: str, gap: float = SIDE_GAP
) -> tuple[Point, Point]:
    """Two points on the named side of an obstacle, straddling it along the travel.

    One point is not enough. A path that detours to a single point beside an
    obstacle and comes straight back is *homotopic to not detouring at all*, so the
    crossing sequence reduces away and both sides of the obstacle produce identical
    geometry -- which is exactly what happened before this existed. Requiring the
    route to reach one point before the obstacle and another after it, both on the
    named side, is a constraint that cannot cancel: the two points sit on opposite
    sides of the obstacle along the direction of travel.
    """
    unit = _unit(direction)
    normal = (unit[1], -unit[0]) if side == "left" else (-unit[1], unit[0])
    centre = obstacle.centroid()
    across = _reach(obstacle, centre, normal) + gap
    along = _reach(obstacle, centre, unit) + gap
    beside = (centre[0] + normal[0] * across, centre[1] + normal[1] * across)
    return (
        (beside[0] - unit[0] * along, beside[1] - unit[1] * along),
        (beside[0] + unit[0] * along, beside[1] + unit[1] * along),
    )


def _unit(direction: Point) -> Point:
    length = math.hypot(direction[0], direction[1])
    if length < 1e-12:
        return (1.0, 0.0)
    return (direction[0] / length, direction[1] / length)


def _reach(obstacle: Obstacle, centre: Point, axis: Point) -> float:
    """How far the obstacle extends from its centroid along ``axis``."""
    return max(
        (vertex[0] - centre[0]) * axis[0] + (vertex[1] - centre[1]) * axis[1]
        for vertex in obstacle.polygon
    )


# ---------------------------------------------------------------------------
# the stretcher
# ---------------------------------------------------------------------------


def stretch_route(
    route: RouteTopology,
    environment: RoutingEnvironment,
    triangulation: Triangulation,
    rules: RouteRules,
) -> StretchResult:
    """Tighten one route's sketch into geometry."""
    from_key = environment.resolve_pad(route.from_)
    to_key = environment.resolve_pad(route.to)
    start = environment.pad_centres.get(from_key) if from_key else None
    end = environment.pad_centres.get(to_key) if to_key else None
    if start is None or end is None:
        missing = route.from_ if start is None else route.to
        raise StretchError(
            f"route {route.key()} starts or ends at {missing!r}, which is not a pad "
            "on this board",
            hint="pads are named REFDES.PAD, e.g. 'J1.3'",
        )

    if any(isinstance(w, ViaHop) for w in route.passes):
        raise StretchError(
            f"route {route.key()} changes layer, which the stretcher does not do yet",
            hint="remove the via hop, or route this net on one layer for now",
        )

    guides = _guide_points(route, environment, start, end, rules)
    crossings, first_triangle = _crossings(guides, triangulation, route)
    reduced = reduce_crossings(crossings)
    sleeve = triangulation.sleeve(reduced, first_triangle)

    diagonals = [
        (triangulation.diagonals[i].a, triangulation.diagonals[i].b) for i in reduced
    ]
    # Each diagonal is oriented by the direction of travel across it, which is the
    # step from the centroid of the triangle before it to the one after. The sleeve
    # supplies exactly one more triangle than there are diagonals, so these line up.
    centroids = [_centroid(triangulation.triangles[t]) for t in sleeve]
    portals: list[Portal] = orient_portals(diagonals, centroids)
    points = tighten(start, end, portals)

    return StretchResult(
        net=route.net,
        layer=route.layer,
        points=points,
        width=rules.track_width,
        crossings=len(reduced),
    )


def _guide_points(
    route: RouteTopology,
    environment: RoutingEnvironment,
    start: Point,
    end: Point,
    rules: RouteRules,
) -> list[Point]:
    """Turn the sketch into the sequence of points the walk must pass through.

    Each ``pass`` waypoint becomes a point just off the named side of its obstacle.
    The direction used to decide "which side" runs from the previous reference to
    the next one, so it follows the route rather than the board's axes.

    The offset is measured from the obstacle *inflated by this route's clearance*,
    not from its physical copper. Measuring from the copper puts the guide point
    inside the clearance zone, where there is no free space to locate it -- every
    sketch with a waypoint then reports as unrealizable, however sound it is.
    """
    passes = [w for w in route.passes if isinstance(w, Pass)]
    if not passes:
        return [start, end]

    margin = rules.clearance + rules.track_width / 2
    resolved: list[Obstacle] = []
    for waypoint in passes:
        obstacle = _find_obstacle(environment, waypoint.obstacle)
        if obstacle is None:
            raise StretchError(
                f"route {route.key()} passes {waypoint.obstacle!r}, which is not on "
                "this board",
                hint="obstacles are pads ('U1.7'), components ('U1') or vias",
            )
        resolved.append(replace(obstacle, polygon=inflate(obstacle.polygon, margin)))

    anchors: list[Point] = [start, *(o.centroid() for o in resolved), end]

    points: list[Point] = [start]
    for index, waypoint in enumerate(passes):
        before = anchors[index]
        after = anchors[index + 2]
        direction = (after[0] - before[0], after[1] - before[1])
        approach, depart = side_gate(resolved[index], direction, waypoint.side)
        points.extend((approach, depart))
    points.append(end)
    return points


def _find_obstacle(environment: RoutingEnvironment, reference: str) -> Obstacle | None:
    """Resolve an obstacle reference, tolerating pads that share a number."""
    obstacle = environment.obstacles.get(reference)
    if obstacle is not None:
        return obstacle
    key = environment.resolve_pad(reference)
    return environment.obstacles.get(key) if key else None


def _crossings(
    guides: list[Point], triangulation: Triangulation, route: RouteTopology
) -> tuple[list[int], int]:
    """Join the guide points into one crossing sequence.

    Each leg is searched independently and the results concatenated. That is valid
    because consecutive legs meet inside one triangle, so the diagonal that ends one
    leg and the diagonal that starts the next both belong to it -- the concatenation
    is still a walk. Forcing the path through a point on the named side of each
    obstacle is what makes the sketch's `left`/`right` real: the homotopy class
    follows from where the walk went.
    """
    located: list[int] = []
    for position, point in enumerate(guides):
        triangle = triangulation.locate(point)
        if triangle is None:
            where = (
                route.from_
                if position == 0
                else route.to
                if position == len(guides) - 1
                else f"waypoint {position}"
            )
            raise StretchError(
                f"route {route.key()}: {where} is not in the routable area",
                hint="the point falls inside another part's clearance, so the route "
                "as sketched cannot exist; pass on the other side, or move the parts "
                "apart",
            )
        located.append(triangle)

    sequence: list[int] = []
    for index in range(len(guides) - 1):
        leg = triangulation.portal_path(
            guides[index], located[index], guides[index + 1], located[index + 1]
        )
        if not leg and located[index] != located[index + 1]:
            raise StretchError(
                f"route {route.key()} cannot reach its next waypoint: the free area "
                "is split in two by other parts' clearances",
                hint="move the parts apart, or route this leg on another layer",
            )
        sequence.extend(leg)
    return sequence, located[0]


def _centroid(triangle: tuple[Point, Point, Point]) -> Point:
    return (
        (triangle[0][0] + triangle[1][0] + triangle[2][0]) / 3,
        (triangle[0][1] + triangle[1][1] + triangle[2][1]) / 3,
    )


def prepare(
    board: object, net: str, layer: str, rules: RouteRules
) -> tuple[RoutingEnvironment, Triangulation]:
    """Build the environment and triangulation for one net on one layer."""
    environment = environment_for(board)
    obstacles = environment.blocking(
        net, layer, clearance=rules.clearance, track_width=rules.track_width
    )
    try:
        triangulation = build_triangulation(
            environment, obstacles, edge_margin=0.5 + rules.track_width / 2
        )
    except FreeSpaceError as exc:
        raise StretchError(str(exc)) from exc
    return environment, triangulation

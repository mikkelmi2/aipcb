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

M8 adds layers. A route that changes layer is cut at its via columns into *legs*,
each of which is exactly the single-layer problem above between two fixed points --
a pad or a via at each end. The funnel does not change at all: a via is simply a
point the rubber band is pinned to, and pinning it is what makes the two layers'
geometry agree (ADR 0007).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Any

from aipcb.route.funnel import Portal, orient_portals, tighten
from aipcb.route.model import Pass, RouteTopology
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
    "LayerGeometry",
    "RouteRules",
    "RoutedConnection",
    "StretchError",
    "StretchResult",
    "Via",
    "environment_for",
    "side_gate",
    "side_point",
    "stretch_guided",
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
    congestion: float = 0.0
    """How hard to avoid narrow gaps. Zero routes purely for length."""

    @property
    def corridor(self) -> float:
        """The width of free space one track needs, clearance on both sides."""
        return self.track_width + 2 * self.clearance


@dataclass(slots=True)
class StretchResult:
    """One tightened leg: a single layer's run between two fixed points.

    A route that never changes layer is one leg, which is what every M7 route was.
    A route that does is several, and the endpoints are what say so: ``start`` and
    ``end`` name a pad (``J1.3#2``) or a via (``via:USB_DP-1``), and are what the
    segment UUIDs are derived from.
    """

    net: str
    layer: str
    points: list[Point] = field(default_factory=list)
    width: float = 0.25
    crossings: int = 0
    """How many triangulation diagonals the reduced homotopy class crosses."""
    start: str = ""
    end: str = ""
    coupled: bool = False
    """Whether this leg is a differential pair's coupled run rather than its fan-out.

    Recorded rather than inferred. Until M13b it *was* inferred, from the leg
    carrying the pair's own width where the fan-out carried the class's -- which
    held on every board here because every controlled-impedance class declared a
    single-ended width narrower than its pair's. The coplanar model derives
    narrower pairs, the fan-out is now clamped to the pair's width so it cannot eat
    the pair's gap, and the two widths became equal. Four separate measurements
    keyed off that inference: the uncoupled-length budget, the wall-hugging scan,
    which copper is fed back between the two halves, and which leg a meander may be
    folded into.
    """

    @property
    def length(self) -> float:
        return sum(
            math.dist(a, b) for a, b in pairwise(self.points)
        )

    @property
    def segments(self) -> list[tuple[Point, Point]]:
        return list(pairwise(self.points))


@dataclass(frozen=True, slots=True)
class Via:
    """A via column: a hole through the board, and the layers it joins.

    The barrel is copper on every layer it passes, not only the two it connects,
    which is what :meth:`span` is for and why a via is an obstacle everywhere it
    goes.
    """

    net: str
    point: Point
    from_layer: str
    to_layer: str
    diameter: float = 0.6
    drill: float = 0.3
    kind: str = "through"
    name: str = ""

    @property
    def radius(self) -> float:
        return self.diameter / 2

    def layers(self) -> tuple[str, str]:
        """The two layers KiCad records, which for a through via are the outers."""
        return (self.from_layer, self.to_layer)


@dataclass(slots=True)
class LayerGeometry:
    """One layer's free space and triangulation, for a particular net's rules."""

    layer: str
    triangulation: Triangulation
    free: Any = None
    """The shapely geometry, kept so a guide point can be projected back into it."""


@dataclass(slots=True)
class RoutedConnection:
    """One connection: its legs, and the vias joining them."""

    net: str
    start: str = ""
    end: str = ""
    legs: list[StretchResult] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    barrel_length: float = 0.0
    """How much conductor the vias add, which length matching has to count."""

    @property
    def copper_length(self) -> float:
        return sum(leg.length for leg in self.legs)

    @property
    def length(self) -> float:
        """Total conductor, barrels included -- what a length-matched net cares about."""
        return self.copper_length + self.barrel_length

    @property
    def layers(self) -> tuple[str, ...]:
        return tuple(leg.layer for leg in self.legs)


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


def stretch_guided(
    start: Point,
    end: Point,
    guides: list[Point],
    geometry: LayerGeometry,
    rules: RouteRules,
    *,
    label: str = "route",
) -> tuple[list[Point], int]:
    """Tighten one leg: from ``start`` to ``end``, through ``guides`` in order.

    The guides are what carry the topology. They come either from a sketch -- a
    point on the named side of each obstacle it must pass -- or from the multilayer
    search, which hands back the midpoint of every gate its path crossed. Either
    way they pin the homotopy class, and the funnel then finds the shortest path in
    it.

    A guide that has fallen inside an obstacle's clearance is *projected* back to
    the nearest free point rather than being fatal. This matters because the search
    and the tightening see slightly different free spaces on purpose: the search
    works in a shared field inflated by the widest clearance on the board, so that
    one set of congestion figures covers every net, while tightening inflates by
    what this net actually needs. A guide near a fat net's hull can be free in one
    and not the other, and dropping the route over a millimetre of disagreement
    would throw away a perfectly good corridor.
    """
    points: list[Point] = [start, *guides, end]
    located: list[int] = []
    kept: list[Point] = []
    for position, point in enumerate(points):
        triangle = geometry.triangulation.locate(point)
        if triangle is None:
            moved = _project(geometry, point)
            triangle = (
                geometry.triangulation.locate(moved) if moved is not None else None
            )
            if triangle is None or not (0 < position < len(points) - 1):
                # An endpoint is never moved. A guide is advisory and can be nudged
                # or dropped, but a route that starts a millimetre from its pad is
                # not a route -- it is a dangling track, which KiCad reports and a
                # person has to chase down. Better to fail here, where the reason is
                # known, than to emit copper that does not connect.
                if 0 < position < len(points) - 1:
                    continue
                which = "start" if position == 0 else "end"
                raise StretchError(
                    f"{label}: the {which} is not in the routable area",
                    hint="the point falls inside another part's clearance, so the "
                    "route as sketched cannot exist; pass on the other side, add a "
                    "via hop, or move the parts apart",
                )
            point = moved  # type: ignore[assignment]
        kept.append(point)
        located.append(triangle)

    # Free space inflated by a clearance comes apart into pieces, and a guide that
    # landed in a different piece from the route cannot be honoured -- there is no
    # path to it at any price. Dropping it keeps the rest of the topology, which is
    # much better than failing the whole route over one advisory point. An *endpoint*
    # in another piece is a different matter, and is a real failure.
    component = geometry.triangulation.component
    home = component(located[0])
    if component(located[-1]) != home:
        raise StretchError(
            f"{label}: the free area is split in two by other parts' clearances",
            hint="move the parts apart, add a via hop, or route this leg on another "
            "layer",
        )
    reachable = [
        (point, triangle)
        for point, triangle in zip(kept, located, strict=True)
        if component(triangle) == home
    ]
    kept = [point for point, _ in reachable]
    located = [triangle for _, triangle in reachable]

    sequence: list[int] = []
    for index in range(len(kept) - 1):
        step = geometry.triangulation.portal_path(
            kept[index],
            located[index],
            kept[index + 1],
            located[index + 1],
            corridor=rules.corridor,
            congestion=rules.congestion,
        )
        if not step and located[index] != located[index + 1]:
            raise StretchError(
                f"{label}: the free area is split in two by other parts' clearances",
                hint="move the parts apart, or route this leg on another layer",
            )
        sequence.extend(step)

    reduced = reduce_crossings(sequence)
    sleeve = geometry.triangulation.sleeve(reduced, located[0])
    diagonals = [
        (geometry.triangulation.diagonals[i].a, geometry.triangulation.diagonals[i].b)
        for i in reduced
    ]
    centroids = [_centroid(geometry.triangulation.triangles[t]) for t in sleeve]
    portals: list[Portal] = orient_portals(diagonals, centroids)
    return tighten(kept[0], kept[-1], portals), len(reduced)


def _project(geometry: LayerGeometry, point: Point) -> Point | None:
    """The nearest point of the free space to one that is not in it."""
    if geometry.free is None:
        return None
    from shapely.geometry import Point as ShapelyPoint
    from shapely.ops import nearest_points

    try:
        nearest = nearest_points(geometry.free, ShapelyPoint(point))[0]
    except (ValueError, AttributeError):  # pragma: no cover - degenerate geometry
        return None
    return (round(nearest.x, 6), round(nearest.y, 6))


def stretch_route(
    route: RouteTopology,
    environment: RoutingEnvironment,
    geometry: Triangulation | dict[str, LayerGeometry],
    rules: RouteRules,
    *,
    stack: object | None = None,
) -> RoutedConnection:
    """Tighten one route's sketch into geometry, across as many layers as it uses.

    ``geometry`` is either a single triangulation -- the M7 case, one layer, no vias
    -- or one :class:`LayerGeometry` per layer, which is what a route with via hops
    needs.
    """
    layers = (
        {route.layer: LayerGeometry(route.layer, geometry)}
        if isinstance(geometry, Triangulation)
        else geometry
    )

    from_key = environment.resolve_pad(route.from_)
    to_key = environment.resolve_pad(route.to)
    start = environment.pad_centres.get(from_key) if from_key else None
    end = environment.pad_centres.get(to_key) if to_key else None
    if start is None or end is None or from_key is None or to_key is None:
        missing = route.from_ if start is None else route.to
        raise StretchError(
            f"route {route.key()} starts or ends at {missing!r}, which is not a pad "
            "on this board",
            hint="pads are named REFDES.PAD, e.g. 'J1.3'",
        )

    for layer in route.layers_used():
        if layer not in layers:
            raise StretchError(
                f"route {route.key()} routes on {layer}, which has no routable area "
                "on this board",
                hint="check the layer exists in `layout.stackup` and is not a plane",
            )

    anchors = _anchor_points(route, environment, start, end, rules, layers)
    connection = RoutedConnection(net=route.net, start=from_key, end=to_key)

    layer = route.layer
    leg_start = start
    leg_start_name = from_key
    pending: list[Pass] = []
    via_number = 0

    def flush(leg_end: Point, leg_end_name: str, on: str) -> None:
        guides = _guide_points(pending, environment, leg_start, leg_end, rules)
        points, crossings = stretch_guided(
            leg_start,
            leg_end,
            guides,
            layers[on],
            rules,
            label=f"route {route.key()}",
        )
        connection.legs.append(
            StretchResult(
                net=route.net,
                layer=on,
                points=points,
                width=rules.track_width,
                crossings=crossings,
                start=leg_start_name,
                end=leg_end_name,
            )
        )

    for index, waypoint in enumerate(route.passes):
        if isinstance(waypoint, Pass):
            pending.append(waypoint)
            continue
        via_number += 1
        point = anchors[index]
        assert point is not None  # _anchor_points fills every via hop or raises
        name = waypoint.name or f"{route.net}-{via_number}"
        via = Via(
            net=route.net,
            point=point,
            from_layer=layer,
            to_layer=waypoint.to_layer,
            diameter=rules.via_diameter,
            drill=rules.via_drill,
            kind=_via_kind(stack, layer, waypoint.to_layer),
            name=name,
        )
        flush(point, f"via:{name}", layer)
        connection.vias.append(via)
        connection.barrel_length += _barrel_length(stack, layer, waypoint.to_layer)
        layer = waypoint.to_layer
        leg_start = point
        leg_start_name = f"via:{name}"
        pending = []

    flush(end, to_key, layer)
    return connection


def _via_kind(stack: object, a: str, b: str) -> str:
    kind = getattr(stack, "via_type", None)
    resolved = kind(a, b) if callable(kind) else None
    return resolved or "through"


def _barrel_length(stack: object, a: str, b: str) -> float:
    length = getattr(stack, "barrel_length", None)
    return float(length(a, b)) if callable(length) else 0.0


def _anchor_points(
    route: RouteTopology,
    environment: RoutingEnvironment,
    start: Point,
    end: Point,
    rules: RouteRules,
    layers: dict[str, LayerGeometry],
) -> list[Point | None]:
    """Where each waypoint sits, with a position chosen for every via hop.

    A sketch never says where a via goes -- that is the whole point of storing
    topology -- so the position has to be derived. It is derived the way a person
    would: put the via between the things on either side of it, and slide it along
    that line until it fits on every layer its barrel passes through.
    """
    anchors: list[Point | None] = []
    for waypoint in route.passes:
        if isinstance(waypoint, Pass):
            obstacle = _find_obstacle(environment, waypoint.obstacle)
            anchors.append(obstacle.centroid() if obstacle is not None else None)
        else:
            anchors.append(None)

    layer = route.layer
    for index, waypoint in enumerate(route.passes):
        if isinstance(waypoint, Pass):
            continue
        before = next(
            (a for a in reversed(anchors[:index]) if a is not None), start
        )
        after = next((a for a in anchors[index + 1 :] if a is not None), end)
        spanned = tuple({layer, waypoint.to_layer})
        anchors[index] = _place_via(
            before, after, spanned, layers, rules.via_diameter / 2 + rules.clearance
        )
        if anchors[index] is None:
            raise StretchError(
                f"route {route.key()} asks for a via between {layer} and "
                f"{waypoint.to_layer}, but nowhere along that leg has room for one",
                hint=f"a {rules.via_diameter} mm via needs "
                f"{rules.via_diameter + 2 * rules.clearance:.2f} mm of clear space on "
                "every layer it passes; move the parts apart or use a smaller via",
            )
        layer = waypoint.to_layer
    return anchors


#: Where along a leg a via is tried, in order. The midpoint first, because that is
#: where a person would put it, then outwards -- never at the very ends, where the
#: via would land on the pad it is escaping from.
_VIA_FRACTIONS = (0.5, 0.4, 0.6, 0.3, 0.7, 0.25, 0.75, 0.2, 0.8, 0.15, 0.85)


def _place_via(
    before: Point,
    after: Point,
    layers: tuple[str, ...],
    geometry: dict[str, LayerGeometry],
    radius: float,
) -> Point | None:
    """The first point along ``before``-``after`` where a via fits on every layer."""
    from shapely.geometry import Point as ShapelyPoint

    for fraction in _VIA_FRACTIONS:
        candidate = (
            before[0] + (after[0] - before[0]) * fraction,
            before[1] + (after[1] - before[1]) * fraction,
        )
        point = ShapelyPoint(candidate)
        if all(
            _room_for(geometry[layer].free, point) >= radius
            for layer in layers
            if layer in geometry
        ):
            return (round(candidate[0], 6), round(candidate[1], 6))
    return None


def _room_for(free: Any, point: Any) -> float:
    """How far a point is from the nearest edge of the free space, or -1 outside."""
    if free is None or not free.covers(point):
        return -1.0
    return float(free.boundary.distance(point))


def _guide_points(
    passes: list[Pass],
    environment: RoutingEnvironment,
    start: Point,
    end: Point,
    rules: RouteRules,
) -> list[Point]:
    """Turn a leg's ``pass`` waypoints into the points the walk must go through.

    Each becomes a point just off the named side of its obstacle. The direction used
    to decide "which side" runs from the previous reference to the next one, so it
    follows the route rather than the board's axes.

    The offset is measured from the obstacle *inflated by this route's clearance*,
    not from its physical copper. Measuring from the copper puts the guide point
    inside the clearance zone, where there is no free space to locate it -- every
    sketch with a waypoint then reports as unrealizable, however sound it is.
    """
    if not passes:
        return []

    margin = rules.clearance + rules.track_width / 2
    resolved: list[Obstacle] = []
    for waypoint in passes:
        obstacle = _find_obstacle(environment, waypoint.obstacle)
        if obstacle is None:
            raise StretchError(
                f"a route passes {waypoint.obstacle!r}, which is not on this board",
                hint="obstacles are pads ('U1.7'), components ('U1') or vias",
            )
        resolved.append(replace(obstacle, polygon=inflate(obstacle.polygon, margin)))

    anchors: list[Point] = [start, *(o.centroid() for o in resolved), end]

    points: list[Point] = []
    for index, waypoint in enumerate(passes):
        before = anchors[index]
        after = anchors[index + 2]
        direction = (after[0] - before[0], after[1] - before[1])
        approach, depart = side_gate(resolved[index], direction, waypoint.side)
        points.extend((approach, depart))
    return points


def _find_obstacle(environment: RoutingEnvironment, reference: str) -> Obstacle | None:
    """Resolve an obstacle reference, tolerating pads that share a number."""
    obstacle = environment.obstacles.get(reference)
    if obstacle is not None:
        return obstacle
    key = environment.resolve_pad(reference)
    return environment.obstacles.get(key) if key else None

def _centroid(triangle: tuple[Point, Point, Point]) -> Point:
    return (
        (triangle[0][0] + triangle[1][0] + triangle[2][0]) / 3,
        (triangle[0][1] + triangle[1][1] + triangle[2][1]) / 3,
    )


def prepare(
    board: object,
    net: str,
    layer: str,
    rules: RouteRules,
    open_pads: frozenset[str] = frozenset(),
) -> tuple[RoutingEnvironment, Triangulation]:
    """Build the environment and triangulation for one net on one layer.

    ``open_pads`` names the pads a route is actually landing on. Everything else --
    including the rest of its own net's pads -- blocks, because a track that clips a
    pad it is only passing leaves a copper sliver.
    """
    environment = environment_for(board)
    obstacles = environment.blocking(
        net,
        layer,
        clearance=rules.clearance,
        track_width=rules.track_width,
        open_pads=open_pads,
    )
    try:
        triangulation = build_triangulation(
            environment, obstacles, edge_margin=0.5 + rules.track_width / 2
        )
    except FreeSpaceError as exc:
        raise StretchError(str(exc)) from exc
    return environment, triangulation

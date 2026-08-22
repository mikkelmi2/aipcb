"""The pieces of geometry every route needs, whichever kind of route it is.

An ordinary connection and half of a differential pair want the same four things: to
know the rules their net class imposes, to see one layer as free space inflated by
what they need, to be tightened through a corridor somebody chose for them, and to
be handed back to the next route as an obstacle. Those live here so that neither the
board-level orchestration nor the pair machinery has to own them, and so that
neither has to import the other to get at them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import pairwise

from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.field import keepout_obstacles
from aipcb.route.obstacles import (
    EDGE_CLEARANCE,
    Obstacle,
    RoutingEnvironment,
    convex_hull,
)
from aipcb.route.stack import RoutingStack
from aipcb.route.stretch import (
    LayerGeometry,
    RouteRules,
    StretchError,
    StretchResult,
    Via,
    stretch_guided,
)
from aipcb.route.triangulate import free_space, triangulate_free

__all__ = [
    "DEFAULT_CONGESTION",
    "class_for",
    "class_name_for",
    "edge_clearance_for",
    "geometry_for",
    "path_length",
    "resample",
    "rules_for",
    "simplify",
    "tighten_leg",
    "track_obstacles",
    "via_obstacles",
    "with_copper",
]

Point = tuple[float, float]

#: A global scale on the router's congestion term, from ``--congestion``. Zero routes
#: for length and via cost alone, ignoring how full a corridor is.
#:
#: In M7 this was the difference between routing a board and stranding a connection.
#: With a second layer to escape to, both settings finish the bundled examples, and
#: what the default buys is measured rather than asserted: on `led-blinker` it is
#: 2 mm less copper and one via fewer, because spending open space first leaves the
#: tight gaps for the routes that had no alternative to them.
DEFAULT_CONGESTION = 1.0


def rules_for(
    netlist: Netlist, net: str, congestion: float = DEFAULT_CONGESTION
) -> RouteRules:
    """The width and clearance a net must be routed to."""
    net_class = class_for(netlist, net)
    return RouteRules(
        track_width=net_class.trace_width_mm,
        clearance=net_class.clearance_mm,
        via_diameter=net_class.via_diameter_mm,
        via_drill=net_class.via_drill_mm,
        congestion=congestion,
    )


def edge_clearance_for(netlist: Netlist) -> float:
    """How far copper must stay from the board edge, and from every cutout.

    The source's figure when it states one, and KiCad's own default otherwise -- so
    a design that says nothing gets the same answer from the router and from DRC,
    which before M9 was true only by coincidence.
    """
    board = netlist.board
    if board is not None and board.edge_clearance is not None:
        return board.edge_clearance
    return EDGE_CLEARANCE


def class_for(netlist: Netlist, net: str) -> NetClass:
    elaborated = netlist.nets.get(net)
    if elaborated is None:
        return NetClass()
    return netlist.net_classes.get(elaborated.net_class, NetClass())


def class_name_for(netlist: Netlist, net: str) -> str:
    elaborated = netlist.nets.get(net)
    return elaborated.net_class if elaborated is not None else "signal"


def track_obstacles(result: StretchResult, name: str, margin: float) -> list[Obstacle]:
    """A finished route, as a set of things later routes must avoid.

    The polygons are the track's *physical* copper -- its centre-line swept by half
    its own width -- and nothing more. Inflating here would be wrong, because how
    much clearance a later route needs depends on that route's width, not this
    one's. Pre-inflating by the routed net's own half-width left every later track
    short by half its own, which DRC duly reported as a clearance violation of
    exactly that size.

    One obstacle per *segment*, not one hull over the whole route. Hulling a bent
    polyline swallows the entire area inside its bend, walling off corridors that
    are perfectly free -- on the examples that alone cost a third of the
    connections, all reported as pads that had mysteriously stopped being reachable.
    """
    obstacles: list[Obstacle] = []
    for index, ((x1, y1), (x2, y2)) in enumerate(result.segments):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = -dy / length * margin, dx / length * margin
        ex, ey = dx / length * margin, dy / length * margin
        corners = (
            (x1 + nx - ex, y1 + ny - ey),
            (x1 - nx - ex, y1 - ny - ey),
            (x2 + nx + ex, y2 + ny + ey),
            (x2 - nx + ex, y2 - ny + ey),
        )
        obstacles.append(
            Obstacle(
                name=f"{name}#{index}",
                polygon=convex_hull(corners),
                net=result.net,
                layers=frozenset({result.layer}),
                kind="track",
            )
        )
    return obstacles


def via_obstacles(via: Via, stack: RoutingStack, name: str) -> list[Obstacle]:
    """A via, as an obstacle on every layer its barrel passes through.

    Every layer, not just the two it connects. The hole is drilled through whatever
    is in the way, so an inner layer that never carries this net still has a
    keep-out where the barrel goes -- and a router that forgets it will happily send
    an inner-layer track through a drill.
    """
    span = stack.barrel_span(via.from_layer, via.to_layer) or (
        via.from_layer,
        via.to_layer,
    )
    ring = tuple(
        (
            via.point[0] + via.radius * math.cos(math.tau * i / 8) / math.cos(math.pi / 8),
            via.point[1] + via.radius * math.sin(math.tau * i / 8) / math.cos(math.pi / 8),
        )
        for i in range(8)
    )
    return [
        Obstacle(
            name=f"{name}@{layer}",
            polygon=ring,
            net=via.net,
            layers=frozenset({layer}),
            kind="via",
        )
        for layer in span
    ]


def with_copper(
    base: RoutingEnvironment,
    placed: list[Obstacle],
    keepouts: Sequence[Obstacle] = (),
) -> RoutingEnvironment:
    """``base`` plus every piece of copper laid so far, with nothing able to hide.

    The obstacle set is a dict keyed by name, and finished copper is a *list* --
    so merging one into the other loses any piece whose name a later piece
    repeats. That is not hypothetical: a differential pair split across two layers
    by a via transition produces one ``RoutedConnection`` per layer, and both name
    their coupled leg after the same two pair terminals. On `examples/pcie-sata`
    four pieces of copper vanished from every obstacle set built after them --
    silently, because a dict assignment cannot fail -- and a repair pass duly
    routed straight through the B.Cu half of `REFCLKP/N`.

    So a name that is already taken is *suffixed* rather than overwritten. Copper
    can only ever be added here, never replaced, whatever it is called: the
    invariant this restores is a property of the merge rather than of everybody's
    naming discipline. :func:`aipcb.route.invariant.crossing_nets` is the check
    that would have caught it, and now does.
    """
    environment = replace(base, obstacles=dict(base.obstacles))
    for index, obstacle in enumerate(placed):
        name = obstacle.name
        if environment.obstacles.get(name, obstacle) is not obstacle:
            name = f"{name}~{index}"
        environment.obstacles[name] = obstacle
    for obstacle in keepouts:
        environment.obstacles[obstacle.name] = obstacle
    return environment


def geometry_for(
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    nets: str | frozenset[str],
    layer: str,
    rules: RouteRules,
    congestion: float,
    open_pads: frozenset[str] = frozenset(),
    clearance_floor: Mapping[str, float] | None = None,
) -> LayerGeometry:
    """This net's own view of one layer: everything else, inflated by what it needs.

    ``clearance_floor`` raises the clearance demanded *by* a named net, above what
    its own class asks for. It exists for one caller: M11d rule 2 re-tightens a
    controlled-impedance pair once, with the clearance of the feature it was
    hugging inflated, to see whether the pair will stand off from it. Empty for
    every other route, which is why every board built before M11 is unchanged.
    """
    environment = with_copper(
        base,
        placed,
        keepout_obstacles(
            netlist.layout, netlist.layout.origin_mm if netlist.layout else (0.0, 0.0)
        ),
    )

    floors = clearance_floor or {}

    def clearance_of(net: str | None) -> float:
        """The clearance the *other* net demands; KiCad enforces the larger."""
        if net is None:
            return 0.0
        own = rules_for(netlist, net, congestion).clearance
        return max(own, floors.get(net, 0.0))

    blocking = environment.blocking(
        nets,
        layer,
        clearance=rules.clearance,
        track_width=rules.track_width,
        clearance_of=clearance_of,
        open_pads=open_pads,
    )
    free = free_space(
        environment, blocking, edge_margin=environment.edge_clearance + rules.track_width / 2
    )
    return LayerGeometry(layer=layer, triangulation=triangulate_free(free), free=free)


def tighten_leg(
    start: Point,
    end: Point,
    guides: list[Point],
    geometry: LayerGeometry,
    rules: RouteRules,
    label: str,
) -> tuple[list[Point], int]:
    """Tighten a leg, keeping the negotiated corridor only while it is worth keeping.

    The guides come from the shared field, which is an approximation made before any
    copper existed: obstacles inflated by the widest clearance on the board, and no
    knowledge of where the routes realized before this one actually landed. Usually
    the plan survives contact with the geometry. Sometimes the corridor it chose has
    since been taken, and following the plan anyway means a long detour round copper
    that was not there when the plan was made.

    So both are tightened and the shorter is kept. The plan wins ties, because
    keeping routes in their negotiated corridors is what makes the congestion
    figures mean anything; but a plan that costs real millimetres against the board
    as it now stands was made on stale information, and saying so is cheaper than
    pretending otherwise.
    """
    planned: tuple[list[Point], int] | None = None
    if guides:
        try:
            planned = stretch_guided(start, end, guides, geometry, rules, label=label)
        except StretchError:
            planned = None
    direct = stretch_guided(start, end, [], geometry, rules, label=label)
    if planned is None:
        return direct
    return planned if path_length(planned[0]) <= path_length(direct[0]) + 1e-9 else direct


def path_length(points: list[Point]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))


def simplify(points: list[Point]) -> list[Point]:
    """Drop the collinear samples, leaving the corners the polyline actually has."""
    from aipcb.route.funnel import signed_area

    kept = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:], strict=False):
        if abs(signed_area(previous, current, following)) > 1e-9:
            kept.append(current)
    kept.append(points[-1])
    return kept


def resample(points: list[Point], step: float) -> list[Point]:
    """A polyline resampled at a fixed spacing, corners kept."""
    out: list[Point] = [points[0]]
    for a, b in pairwise(points):
        span = math.dist(a, b)
        steps = max(int(span / step), 1)
        out.extend(
            (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            for i in range(1, steps + 1)
        )
    return out

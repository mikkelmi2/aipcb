"""Routing a whole board: which connections to make, in what order, avoiding what.

Tightening one route is a pure geometric question. Routing a board is not, because
every route that lands becomes an obstacle for the ones after it. This module
handles that part:

* decide **what to connect** -- a net with *n* pads needs *n-1* routes, chosen as a
  minimum spanning tree over the pad positions, so the total copper is short and the
  choice is deterministic;
* decide **in what order** -- shortest connections first, because a short route has
  the fewest alternatives and should get to claim its corridor while one exists;
* **feed each finished route back** as an obstacle, so later routes go around it.

Routing nets independently and hoping they miss each other does not work: on the USB
example every net takes a near-straight line and four of them cross. Sequential
routing with feedback is the classical answer and is what makes the result
DRC-clean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.model import RouteTopology
from aipcb.route.obstacles import Obstacle, convex_hull, extract_obstacles
from aipcb.route.stretch import (
    RouteRules,
    StretchError,
    StretchResult,
    stretch_route,
)
from aipcb.route.triangulate import FreeSpaceError, build_triangulation

__all__ = ["RoutedBoard", "route_board", "rules_for", "spanning_routes"]

Point = tuple[float, float]

#: KiCad's default board-edge clearance. Copper closer than this to the outline is
#: a DRC error, so the routable area is eroded by it before anything is routed.
EDGE_CLEARANCE = 0.5


@dataclass(slots=True)
class RoutedBoard:
    """The outcome of routing a board."""

    routed: list[StretchResult] = field(default_factory=list)
    endpoints: list[tuple[str, str]] = field(default_factory=list)
    """The pads each routed result connects, positionally matching ``routed``."""
    failed: list[tuple[RouteTopology, str]] = field(default_factory=list)
    total_length: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed

    def with_endpoints(self) -> list[tuple[StretchResult, str, str]]:
        return [
            (result, start, end)
            for result, (start, end) in zip(self.routed, self.endpoints, strict=True)
        ]

    def summary(self) -> dict[str, object]:
        return {
            "routed": len(self.routed),
            "failed": len(self.failed),
            "length_mm": round(self.total_length, 3),
            "nets": sorted({r.net for r in self.routed}),
        }


#: How hard the auto-router avoids narrow gaps. Measured rather than guessed: on
#: the bundled examples, zero routes one connection fewer *and* lays 4 mm more
#: copper than 1.0 does, because the shortest route takes the one gap a later net
#: had no alternative to. Higher values did not help further.
DEFAULT_CONGESTION = 1.0


def rules_for(
    netlist: Netlist, net: str, congestion: float = DEFAULT_CONGESTION
) -> RouteRules:
    """The width and clearance a net must be routed to."""
    elaborated = netlist.nets.get(net)
    default = NetClass()
    net_class = (
        netlist.net_classes.get(elaborated.net_class, default)
        if elaborated is not None
        else default
    )
    return RouteRules(
        track_width=net_class.trace_width_mm,
        clearance=net_class.clearance_mm,
        via_diameter=net_class.via_diameter_mm,
        via_drill=net_class.via_drill_mm,
        congestion=congestion,
    )


def spanning_routes(
    net: str, pads: list[str], centres: dict[str, Point], layer: str
) -> list[RouteTopology]:
    """Connect a net's pads with a minimum spanning tree.

    A star from one pad would be simpler and much longer. Prim's algorithm over
    Euclidean distance gives the shortest set of two-pad connections that ties the
    net together; ties break on pad name so the result never depends on ordering.
    """
    if len(pads) < 2:
        return []

    ordered = sorted(pads)
    inside = {ordered[0]}
    edges: list[tuple[str, str]] = []

    while len(inside) < len(ordered):
        best: tuple[float, str, str] | None = None
        for source in sorted(inside):
            for target in ordered:
                if target in inside:
                    continue
                distance = math.dist(centres[source], centres[target])
                candidate = (distance, source, target)
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            break
        _, source, target = best
        inside.add(target)
        edges.append((source, target))

    return [
        RouteTopology.model_validate(
            {"net": net, "from": source, "to": target, "layer": layer}
        )
        for source, target in edges
    ]


#: Lower routes earlier. Nets with an impedance target or a fine-pitch escape have
#: the least freedom, so they choose their corridors first.
_CLASS_ORDER = {
    "diff_pair": 0, "usb": 0, "high_speed": 1, "clock": 1,
    "analog": 2, "signal": 3, "power": 4, "ground": 5,
}


def _priority(netlist: Netlist, net: str) -> int:
    elaborated = netlist.nets.get(net)
    if elaborated is None:
        return 3
    return _CLASS_ORDER.get(elaborated.net_class, 3)


def _track_obstacles(result: StretchResult, name: str, margin: float) -> list[Obstacle]:
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


def route_board(
    board: SNode,
    netlist: Netlist,
    report: Report,
    *,
    layer: str = "F.Cu",
    topologies: tuple[RouteTopology, ...] = (),
    congestion: float = DEFAULT_CONGESTION,
) -> RoutedBoard:
    """Route every net that needs it, shortest connections first.

    Explicit sketches from ``layout.routes`` are honoured; every other connection
    gets the shortest topology the triangulation allows.
    """
    outcome = RoutedBoard()
    base = extract_obstacles(board)

    explicit = {(t.net, t.from_, t.to) for t in topologies}
    planned: list[RouteTopology] = list(topologies)

    for net in sorted(netlist.nets):
        # Every pad *instance* on the net, so duplicated pad numbers -- a thermal
        # tab sharing its pin's number -- each get connected.
        pads = sorted(
            key for key, pad_net in base.pad_nets.items() if pad_net == net
        )
        for candidate in spanning_routes(net, pads, base.pad_centres, layer):
            if (candidate.net, candidate.from_, candidate.to) in explicit:
                continue
            if (candidate.net, candidate.to, candidate.from_) in explicit:
                continue
            planned.append(candidate)

    def span(route: RouteTopology) -> float:
        a = base.pad_centres.get(base.resolve_pad(route.from_) or "")
        b = base.pad_centres.get(base.resolve_pad(route.to) or "")
        return math.dist(a, b) if a and b else float("inf")

    # Critical nets first, then shortest. This is what a person does, and for the
    # same reason: a differential pair escaping a fine-pitch connector has almost no
    # freedom, while a ground hop between two capacitors has plenty. Ordering purely
    # by length lets the easy routes take the corridors the hard ones needed --
    # measurably, on the USB example, it costs six connections.
    planned.sort(key=lambda r: (_priority(netlist, r.net), span(r), r.key()))

    def clearance_of(net: str | None) -> float:
        """The clearance the *other* net demands; KiCad enforces the larger."""
        if net is None:
            return 0.0
        return rules_for(netlist, net, congestion).clearance

    placed: list[Obstacle] = []
    for route in planned:
        rules = rules_for(netlist, route.net, congestion)
        environment = extract_obstacles(board)
        for obstacle in placed:
            environment.obstacles[obstacle.name] = obstacle

        blocking = environment.blocking(
            route.net,
            route.layer,
            clearance=rules.clearance,
            track_width=rules.track_width,
            clearance_of=clearance_of,
        )
        try:
            triangulation = build_triangulation(
                environment,
                blocking,
                edge_margin=EDGE_CLEARANCE + rules.track_width / 2,
            )
            result = stretch_route(route, environment, triangulation, rules)
        except (StretchError, FreeSpaceError) as exc:
            reason = getattr(exc, "message", str(exc))
            outcome.failed.append((route, reason))
            report.warning(
                "route-failed",
                f"could not route {route.net} from {route.from_} to {route.to}: "
                f"{reason}",
                hint=getattr(exc, "hint", None)
                or "try another layer, move the parts apart, or give the route an "
                "explicit topology under `layout.routes`",
                net=route.net,
            )
            continue

        outcome.routed.append(result)
        outcome.endpoints.append((route.from_, route.to))
        outcome.total_length += result.length
        placed.extend(
            _track_obstacles(result, f"track:{route.key()}", rules.track_width / 2)
        )

    return outcome

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
from dataclasses import dataclass, field, replace
from itertools import pairwise

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.diffpair import DiffPair, find_pairs, skew_of, split_centre_line
from aipcb.route.model import RouteTopology
from aipcb.route.obstacles import (
    Obstacle,
    RoutingEnvironment,
    convex_hull,
    extract_obstacles,
)
from aipcb.route.stretch import (
    RouteRules,
    StretchError,
    StretchResult,
    stretch_points,
    stretch_route,
)
from aipcb.route.triangulate import FreeSpaceError, build_triangulation

__all__ = ["RoutedBoard", "route_board", "rules_for", "spanning_routes"]

Point = tuple[float, float]

#: KiCad's default board-edge clearance. Copper closer than this to the outline is
#: a DRC error, so the routable area is eroded by it before anything is routed.
EDGE_CLEARANCE = 0.5

#: How much of a pair may be fan-out before it stops being worth calling coupled.
#: A short uncoupled breakout at each end is normal and unavoidable -- pads are
#: never at the pair's own pitch -- so this is a judgement call rather than a
#: physical limit, and the actual figure is reported either way.
MAX_UNCOUPLED = 0.25


@dataclass(slots=True)
class RoutedBoard:
    """The outcome of routing a board."""

    routed: list[StretchResult] = field(default_factory=list)
    endpoints: list[tuple[str, str]] = field(default_factory=list)
    pairs: list[DiffPair] = field(default_factory=list)
    """Differential pairs that were routed as pairs."""
    skew: dict[str, float] = field(default_factory=dict)
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

    # Pairs go first and are routed as pairs. Their impedance comes from the
    # coupling between the two halves, so routing them independently would be
    # routing something else that happens to have the same netlist.
    done: set[tuple[str, str, str]] = set()
    for pair in find_pairs(netlist, base, report):
        results = _route_pair(board, netlist, pair, layer, congestion, placed, report)
        if results is None:
            continue
        for result, start, end in results:
            outcome.routed.append(result)
            outcome.endpoints.append((start, end))
            outcome.total_length += result.length
            done.add((result.net, start, end))
            placed.extend(
                _track_obstacles(
                    result, f"track:{result.net}/{start}>{end}", result.width / 2
                )
            )
        outcome.pairs.append(pair)

    for route in planned:
        if (route.net, route.from_, route.to) in done:
            continue
        if (route.net, route.to, route.from_) in done:
            continue
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


def _route_pair(
    board: SNode,
    netlist: Netlist,
    pair: DiffPair,
    layer: str,
    congestion: float,
    placed: list[Obstacle],
    report: Report,
) -> list[tuple[StretchResult, str, str]] | None:
    """Route both halves of a pair from one tightened centre-line."""
    environment = extract_obstacles(board)
    for obstacle in placed:
        environment.obstacles[obstacle.name] = obstacle

    both = frozenset({pair.positive, pair.negative})
    base_rules = rules_for(netlist, pair.positive, congestion)
    # The centre-line is tightened as though it were one fat track: both traces and
    # the gap between them. Whatever clears that corridor clears the pair.
    centre_rules = replace(base_rules, track_width=pair.corridor)

    blocking = environment.blocking(
        both,
        layer,
        clearance=centre_rules.clearance,
        track_width=centre_rules.track_width,
        clearance_of=lambda net: rules_for(netlist, net, congestion).clearance
        if net
        else 0.0,
    )

    start = _midpoint(environment, pair.starts)
    end = _midpoint(environment, pair.ends)
    if start is None or end is None:
        return None

    try:
        triangulation = build_triangulation(
            environment,
            blocking,
            edge_margin=EDGE_CLEARANCE + centre_rules.track_width / 2,
        )
        centre = stretch_points(
            start, end, triangulation, centre_rules, label=f"pair {pair.key()}"
        )
    except (StretchError, FreeSpaceError) as exc:
        reason = getattr(exc, "message", str(exc))
        report.warning(
            "diff-pair-not-coupled",
            f"could not route {pair.key()} as a coupled pair: {reason}",
            hint="the two halves will be routed separately, so the gap and the "
            "skew budget are no longer guaranteed",
            net=pair.positive,
        )
        return None

    left, right = split_centre_line(centre, pair.pitch)
    assignment = _assign_halves(environment, pair, left, right)
    if assignment is None:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()} would have to cross over between its two ends, which "
            "coupled routing does not build yet",
            hint="the halves will be routed separately, so the gap and the skew "
            "budget are no longer guaranteed; swapping the two pads at one end "
            "would remove the crossover",
            net=pair.positive,
        )
        return None

    results: list[tuple[StretchResult, str, str]] = []
    for net, points, start_pad, end_pad in assignment:
        joined = _join_to_pads(environment, points, start_pad, end_pad)
        results.append(
            (
                StretchResult(
                    net=net, layer=layer, points=joined, width=pair.width,
                    crossings=len(centre),
                ),
                start_pad,
                end_pad,
            )
        )

    uncoupled = _uncoupled_fraction(results, centre)
    if uncoupled > MAX_UNCOUPLED:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()} would be {uncoupled * 100:.0f}% fan-out: its pads are too "
            "far apart at one end for the pair to run coupled between them",
            hint="place the two halves' end components side by side -- a `group` "
            "constraint does that -- or accept the halves being routed separately",
            net=pair.positive,
        )
        return None

    if not _halves_are_clean(results[0][0].points, results[1][0].points):
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()} could not be fanned out to its pads without the two "
            "halves touching",
            hint="the halves will be routed separately; moving the pads further "
            "apart, or giving the pair a wider gap, usually resolves it",
            net=pair.positive,
        )
        return None

    # The tightener guarantees clearance for the centre-line, but the fan-out at
    # each end is constructed rather than tightened, so it has to be checked. A
    # coupled pair that lands DRC violations is worse than two separately routed
    # nets, whatever its gap is.
    offender = _fan_out_collision(
        results, environment, both, layer, base_rules, netlist, congestion
    )
    if offender is not None:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()}'s fan-out to its pads would not clear {offender}",
            hint="the halves will be routed separately, which keeps the board legal "
            "at the cost of the gap; placing the end components at the pair's own "
            "pitch removes the fan-out entirely",
            net=pair.positive,
        )
        return None

    report.info(
        "diff-pair-coupled",
        f"{pair.key()} routed as a coupled pair with a {pair.gap} mm gap; "
        f"{uncoupled * 100:.0f}% of each half is fan-out at the ends",
        net=pair.positive,
    )

    skew = skew_of(results[0][0], results[1][0])
    if pair.max_skew is not None and skew > pair.max_skew:
        report.warning(
            "diff-pair-skew",
            f"{pair.key()} is {skew:.3f} mm out of length, against a "
            f"{pair.max_skew:.3f} mm budget",
            hint="the mismatch comes from the outside of each bend being longer; "
            "shorten the run, straighten it, or raise `max_skew_mm`",
            net=pair.positive,
        )
    return results


def _fan_out_collision(
    results: list[tuple[StretchResult, str, str]],
    environment: RoutingEnvironment,
    nets: frozenset[str],
    layer: str,
    rules: RouteRules,
    netlist: Netlist,
    congestion: float,
) -> str | None:
    """Name the first obstacle a coupled pair's copper would not clear."""
    from shapely.geometry import LineString
    from shapely.geometry import Polygon as ShapelyPolygon

    blocking = environment.blocking(
        nets,
        layer,
        clearance=rules.clearance,
        track_width=rules.track_width,
        clearance_of=lambda net: rules_for(netlist, net, congestion).clearance
        if net
        else 0.0,
    )
    for result, _, _ in results:
        if len(result.points) < 2:
            continue
        centre = LineString(result.points)
        for obstacle in blocking:
            if len(obstacle.polygon) < 3:
                continue
            if centre.intersects(ShapelyPolygon(obstacle.polygon)):
                return obstacle.name
    return None


def _uncoupled_fraction(
    results: list[tuple[StretchResult, str, str]], centre: list[Point]
) -> float:
    """How much of each half is fan-out rather than coupled run.

    A pair is only a pair where the two halves are actually side by side. If the
    pads at one end are far apart -- two separate resistors rather than two pins of
    one connector -- most of the "pair" is really two independent breakouts, and the
    impedance and skew guarantees do not hold across them. Measuring the excess of
    each half over the centre-line says exactly how much.
    """
    centre_length = sum(math.dist(a, b) for a, b in pairwise(centre))
    if centre_length <= 0:
        return 1.0
    worst = max(result.length for result, _, _ in results)
    return max(0.0, (worst - centre_length) / worst)


def _midpoint(
    environment: RoutingEnvironment, pads: tuple[str, str]
) -> Point | None:
    first = environment.pad_centres.get(environment.resolve_pad(pads[0]) or "")
    second = environment.pad_centres.get(environment.resolve_pad(pads[1]) or "")
    if first is None or second is None:
        return None
    return ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)


def _assign_halves(
    environment: RoutingEnvironment,
    pair: DiffPair,
    left: list[Point],
    right: list[Point],
) -> list[tuple[str, list[Point], str, str]] | None:
    """Decide which offset polyline belongs to which net.

    The choice has to hold at *both* ends. A pair whose pads swap sides between the
    connector and the destination has to cross over somewhere, and a crossover is a
    deliberate construction -- two short opposed jogs with the gap maintained
    through them -- not something that can be had by joining the offsets to
    whichever pad is nearest. When the two ends disagree, this returns ``None`` and
    the caller falls back to routing the halves separately, which is worse but
    honest.
    """
    if not left or not right:
        return None
    located: dict[str, Point] = {}
    for name in (*pair.starts, *pair.ends):
        centre = environment.pad_centres.get(environment.resolve_pad(name) or "")
        if centre is None:
            return None
        located[name] = centre

    start = located[pair.starts[0]]
    finish = located[pair.ends[0]]
    start_left_is_positive = math.dist(left[0], start) <= math.dist(right[0], start)
    end_left_is_positive = math.dist(left[-1], finish) <= math.dist(right[-1], finish)
    if start_left_is_positive != end_left_is_positive:
        return None

    near, far = (left, right) if start_left_is_positive else (right, left)
    return [
        (pair.positive, near, pair.starts[0], pair.ends[0]),
        (pair.negative, far, pair.starts[1], pair.ends[1]),
    ]


def _halves_are_clean(first: list[Point], second: list[Point]) -> bool:
    """Whether the two halves stay apart along their whole length.

    Joining an offset polyline to its pads adds a short fan-out at each end, and
    those fan-outs are the one part of a coupled pair that is not produced by the
    tightener. If they cross, the pair is a short, so it is checked rather than
    assumed.
    """
    from shapely.geometry import LineString

    if len(first) < 2 or len(second) < 2:
        return False
    return not LineString(first).intersects(LineString(second))


def _join_to_pads(
    environment: RoutingEnvironment, points: list[Point], start_pad: str, end_pad: str
) -> list[Point]:
    """Fan the offset polyline's ends out to the pads they actually land on."""
    start = environment.pad_centres.get(environment.resolve_pad(start_pad) or "")
    end = environment.pad_centres.get(environment.resolve_pad(end_pad) or "")
    joined = list(points)
    if start is not None:
        joined[0] = start
    if end is not None:
        joined[-1] = end
    return joined

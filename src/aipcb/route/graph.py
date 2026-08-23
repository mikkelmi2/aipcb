# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Shortest paths across every layer at once, with vias as ordinary moves.

M7 searched one layer's triangulation. This searches all of them, joined at via
sites, in a single A\\*. That is the difference between a router that *may* use
another layer and one that *chooses* to: layer, via position and corridor come out
of one minimisation rather than three sequential decisions.

The node is a position in the free space, and there are three kinds:

* at the midpoint of a triangulation gate (the M7 node, which carries the homotopy
  information -- see :doc:`../topology`);
* at a via site, having just come through the barrel;
* at a pad escape, where a route starts or ends.

Every node knows which triangle it is in, and every move goes from a node to
another node in the same triangle -- across a gate to the next triangle, or down a
via to a triangle on another layer. That uniformity is what keeps the search small
enough to run a dozen times in a negotiation loop.

The cost of a move is documented in :doc:`../routing-costs`, and lives in
:mod:`aipcb.route.costs` so it can be read in one place and overridden in one place.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from itertools import pairwise

from aipcb.model.layout import NetClass
from aipcb.route.costs import DEFAULT_COSTS, CostModel
from aipcb.route.field import LayeredField

__all__ = [
    "Leg",
    "PathVia",
    "RoutePath",
    "SearchRules",
    "Terminal",
    "search_path",
]

Point = tuple[float, float]

#: Node kinds. Small ints rather than an enum because they sit inside the heap key,
#: which is compared millions of times.
_GATE = 0
_VIA = 1
_TERMINAL = 2

#: (layer index, kind, index, triangle)
Node = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Terminal:
    """One end of a connection: a pad, and the points a route may leave it from."""

    name: str
    """The pad instance, e.g. ``J1.3#2``."""
    centre: Point
    escapes: tuple[tuple[str, Point, int], ...]
    """(layer, point, triangle) for every way out of this pad."""


@dataclass(frozen=True, slots=True)
class SearchRules:
    """Everything about a net that the search needs to know."""

    net: str
    net_class: NetClass
    demand: float
    """Millimetres of cut a crossing consumes: the track's width plus its clearance."""
    via_radius: float
    layers: tuple[str, ...]
    """The layers this net may use, cheapest first is not implied -- see the stack."""
    present: float = 1.0
    """This iteration's congestion factor. Rises as the negotiation proceeds."""
    congestion_weight: float = 1.0
    """A global scale on the congestion term, from ``--congestion``."""
    max_vias: int = 4
    """A ceiling on layer changes per connection, so the search stays finite-ish."""


@dataclass(frozen=True, slots=True)
class PathVia:
    """A layer change: a via column at a chosen site."""

    site: int
    point: Point
    from_layer: str
    to_layer: str


@dataclass(slots=True)
class Leg:
    """One same-layer run of a connection, between two fixed points."""

    layer: str
    start: Point
    end: Point
    guides: list[Point] = field(default_factory=list)
    """Gate midpoints along the way, which is what pins the homotopy class."""
    crossings: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RoutePath:
    """What the search found: legs joined by vias, and the cuts it consumes."""

    legs: list[Leg] = field(default_factory=list)
    vias: list[PathVia] = field(default_factory=list)
    cost: float = 0.0
    start: str = ""
    end: str = ""

    @property
    def layers(self) -> tuple[str, ...]:
        return tuple(leg.layer for leg in self.legs)

    def crossings(self) -> dict[str, list[int]]:
        """Every cut this path crosses, by layer, with repeats kept.

        Repeats are kept deliberately: a route that crosses the same cut twice
        consumes it twice, and pretending otherwise is how a corridor ends up with
        more copper in it than it has room for.
        """
        out: dict[str, list[int]] = {}
        for leg in self.legs:
            out.setdefault(leg.layer, []).extend(leg.crossings)
        return out

    def length(self) -> float:
        total = 0.0
        for leg in self.legs:
            points = [leg.start, *leg.guides, leg.end]
            total += sum(math.dist(a, b) for a, b in pairwise(points))
        return total


def search_path(
    field_: LayeredField,
    source: Terminal,
    target: Terminal,
    rules: SearchRules,
    costs: CostModel = DEFAULT_COSTS,
) -> RoutePath | None:
    """The cheapest way from ``source`` to ``target`` across all allowed layers.

    Returns ``None`` when no path exists at any price -- which on a board usually
    means the pad is walled in on every layer it reaches, and is reported as such
    rather than as a routing failure with no explanation.
    """
    allowed = tuple(name for name in rules.layers if name in field_.layers)
    if not allowed:
        return None
    layer_index = {name: i for i, name in enumerate(allowed)}

    sources = [
        (layer, point, triangle)
        for layer, point, triangle in source.escapes
        if layer in layer_index
    ]
    goals: dict[tuple[int, int], list[Point]] = {}
    for layer, point, triangle in target.escapes:
        if layer in layer_index:
            goals.setdefault((layer_index[layer], triangle), []).append(point)
    if not sources or not goals:
        return None

    goal_points = [point for _, point, _ in target.escapes if point]

    def heuristic(point: Point) -> float:
        return min(math.dist(point, g) for g in goal_points) if goal_points else 0.0

    stack = field_.stack
    net_class = rules.net_class
    layer_cost = [stack.layer_penalty(name, net_class) for name in allowed]
    present = rules.present * rules.congestion_weight
    via_room = field_.room_needed(rules.via_radius)

    positions: dict[Node, Point] = {}
    best: dict[Node, float] = {}
    previous: dict[Node, Node] = {}
    hops: dict[Node, int] = {}
    """How many via columns the best path to each node has been through."""
    queue: list[tuple[float, float, Node]] = []

    def position(node: Node) -> Point:
        return positions[node]

    def push(
        node: Node, point: Point, cost: float, came_from: Node | None, vias: int
    ) -> None:
        if cost >= best.get(node, math.inf) - 1e-12:
            return
        best[node] = cost
        positions[node] = point
        hops[node] = vias
        if came_from is not None:
            previous[node] = came_from
        heapq.heappush(queue, (cost + heuristic(point), cost, node))

    for ordinal, (layer, point, triangle) in enumerate(sorted(sources)):
        node = (layer_index[layer], _TERMINAL, ordinal, triangle)
        push(node, point, layer_cost[layer_index[layer]], None, 0)

    finish: tuple[float, Node, Point] | None = None

    while queue:
        estimate, cost, node = heapq.heappop(queue)
        if cost > best.get(node, math.inf) + 1e-12:
            continue
        if finish is not None and estimate >= finish[0] - 1e-12:
            break

        layer_i, kind, _index, triangle = node
        here = position(node)
        vias_here = hops.get(node, 0)

        reached = goals.get((layer_i, triangle))
        if reached:
            landing = min(reached, key=lambda g: (math.dist(here, g), g))
            total = cost + math.dist(here, landing)
            if finish is None or total < finish[0] - 1e-12:
                finish = (total, node, landing)

        layer = allowed[layer_i]
        layer_field = field_.layers[layer]
        triangulation = layer_field.triangulation

        for edge in triangulation.by_triangle.get(triangle, ()):
            if kind == _GATE and edge == _index:
                continue
            if not layer_field.fits(edge, rules.demand):
                continue
            midpoint = layer_field.midpoints[edge]
            step = math.dist(here, midpoint)
            occupancy = layer_field.occupancy(edge, rules.demand)
            extra = (
                costs.congestion_cap
                if occupancy > 1.0
                else step * present * occupancy
            )
            move = (
                step
                + extra
                + layer_field.history[edge]
                + stack.direction_penalty(
                    layer, midpoint[0] - here[0], midpoint[1] - here[1]
                )
            )
            onward = (
                layer_i,
                _GATE,
                edge,
                triangulation.diagonals[edge].other(triangle),
            )
            push(onward, midpoint, cost + move, node, vias_here)

        if vias_here >= rules.max_vias:
            continue
        for site_index in field_.sites_in(layer, triangle):
            site = field_.sites[site_index]
            for other_i, other in enumerate(allowed):
                if other == layer:
                    continue
                span = stack.barrel_span(layer, other)
                if not span or not site.fits(span, via_room):
                    continue
                landing_triangle = site.triangle.get(other)
                if landing_triangle is None:
                    continue
                move = (
                    math.dist(here, site.point)
                    + costs.via_cost_mm
                    + layer_cost[other_i]
                )
                onward = (other_i, _VIA, site_index, landing_triangle)
                push(onward, site.point, cost + move, node, vias_here + 1)

    if finish is None:
        return None
    return _rebuild(finish, previous, positions, allowed, field_, source, target)


def _rebuild(
    finish: tuple[float, Node, Point],
    previous: dict[Node, Node],
    positions: dict[Node, Point],
    allowed: tuple[str, ...],
    field_: LayeredField,
    source: Terminal,
    target: Terminal,
) -> RoutePath:
    """Turn the search's node chain into legs and vias."""
    total, node, landing = finish
    chain: list[Node] = []
    cursor: Node | None = node
    while cursor is not None:
        chain.append(cursor)
        cursor = previous.get(cursor)
    chain.reverse()

    path = RoutePath(cost=total, start=source.name, end=target.name)
    current: Leg | None = None
    for step in chain:
        layer_i, kind, index, _triangle = step
        layer = allowed[layer_i]
        point = positions[step]
        if current is None:
            # The first node is the pad escape. It goes in as a guide, not as the
            # leg's start: the leg starts at the pad itself, and the escape is what
            # says which way out of the pad it took.
            current = Leg(layer=layer, start=source.centre, end=point)
            current.guides.append(point)
            continue
        if kind == _VIA:
            current.end = point
            path.legs.append(current)
            path.vias.append(
                PathVia(
                    site=index,
                    point=point,
                    from_layer=current.layer,
                    to_layer=layer,
                )
            )
            current = Leg(layer=layer, start=point, end=point)
        if kind == _GATE:
            current.guides.append(point)
            current.crossings.append(index)
    if current is not None:
        current.guides.append(landing)
        current.end = target.centre
        path.legs.append(current)
    return path

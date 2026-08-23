# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The layered routing field: one triangulation per layer, joined by via columns.

This is M8a. Where M7 had a single triangulation of one layer's free space, a
multilayer router needs one per copper layer, a way of stepping between them, and a
notion of how full each corridor is that survives a route being ripped up.

Three ideas, in the order they matter:

**A cut has a capacity.** Every interior edge of a triangulation is a *cut* across
the free space, and Maley's realizability criterion says a set of topologies can be
turned into legal geometry exactly when no cut is over-subscribed. So each edge
carries a capacity in millimetres and each route crossing it consumes its own width
plus its clearance. That makes "is this board routable" a local, checkable question
-- and, crucially, an *undoable* one: ripping a route up subtracts its demand and
nothing else.

**A via is a column.** It is not a point on two layers; it is a hole through the
board. A via node exists as an obstacle on every copper layer its barrel passes
through, whether or not it carries signal there, and it joins the triangulations of
the layers it connects.

**Congestion is a number, not a shape.** M7 made congestion implicit in the
geometry -- a laid track narrowed the next net's gates. That is elegant and it
cannot be reversed, which makes negotiation impossible. Here it is explicit.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from shapely.geometry import Point as ShapelyPoint

from aipcb.model.layout import Keepout, Layout
from aipcb.route.obstacles import (
    Obstacle,
    RoutingEnvironment,
    inflate,
)
from aipcb.route.stack import RoutingStack
from aipcb.route.triangulate import (
    FreeSpaceError,
    Triangulation,
    free_space,
    triangulate_free,
)

__all__ = [
    "LayerField",
    "LayeredField",
    "ViaSite",
    "build_field",
    "keepout_obstacles",
]

Point = tuple[float, float]

#: How far apart candidate via sites are placed where the free space is open, as a
#: multiple of the via pitch. The brief's figure: fine enough that a via has
#: somewhere sensible to land, coarse enough that the search does not drown in
#: near-identical alternatives whose difference the stretcher will erase anyway.
VIA_SITE_SPACING = 2.0

#: Escape points are placed this far outside a pad's inflated hull, in millimetres.
#: Far enough to be unambiguously in free space, near enough not to move the
#: homotopy class the search picks.
ESCAPE_GAP = 0.02

#: How many via candidates any one triangle offers the search.
MAX_SITES_PER_TRIANGLE = 32

#: Escape rings are tried at these multiples of the clearance, snug first.
ESCAPE_RINGS = (1.0, 2.5, 5.0)


@dataclass(slots=True)
class LayerField:
    """One copper layer: its free space, its triangulation, and how full it is."""

    layer: str
    free: Any
    triangulation: Triangulation
    capacity: list[float] = field(default_factory=list)
    """Per diagonal, in millimetres of track-plus-clearance it can carry."""
    used: list[float] = field(default_factory=list)
    history: list[float] = field(default_factory=list)
    """PathFinder's history term, in millimetres, per diagonal."""
    midpoints: list[Point] = field(default_factory=list)
    _tree: Any = None
    """A lazily built R-tree over the diagonals, for charging finished geometry."""

    def occupancy(self, edge: int, demand: float) -> float:
        """How full this cut would be with ``demand`` more millimetres on it."""
        capacity = self.capacity[edge]
        if capacity <= 0:
            return math.inf
        return (self.used[edge] + demand) / capacity

    def fits(self, edge: int, demand: float) -> bool:
        """Whether a single track of this width could ever cross this cut.

        Not a congestion question but a geometric one, and so not negotiable: a gate
        narrower than one track is not a corridor that a busy net might squeeze
        through later, it is a wall.
        """
        return self.capacity[edge] >= demand

    def over_subscribed(self) -> list[int]:
        return [
            edge
            for edge, used in enumerate(self.used)
            if used > self.capacity[edge] + 1e-9
        ]

    def cuts_crossed(self, points: list[Point]) -> list[int]:
        """Which cuts a finished polyline crosses.

        Routes that came out of the search already know their crossings. This is for
        the ones that did not -- a sketch written by hand, tightened straight to
        geometry -- so that their demand can still be charged to the corridors they
        use and the nets negotiating around them see a truthful picture.
        """
        if len(points) < 2:
            return []
        from shapely import STRtree
        from shapely.geometry import LineString

        if self._tree is None:
            self._tree = STRtree(
                [LineString((d.a, d.b)) for d in self.triangulation.diagonals]
            )
        line = LineString(points)
        tree = self._tree
        return sorted(
            int(i)
            for i in tree.query(line)
            if line.intersects(tree.geometries[int(i)])
        )


@dataclass(frozen=True, slots=True)
class ViaSite:
    """Somewhere a via could go, and how much room it has on each layer."""

    index: int
    point: Point
    room: dict[str, float]
    """Layer to the distance from this point to the nearest obstacle on it."""
    triangle: dict[str, int]
    """Layer to the triangle containing this point."""

    def fits(self, layers: tuple[str, ...], radius: float) -> bool:
        """Whether a via of this radius clears everything on every layer it passes."""
        return all(self.room.get(layer, -1.0) >= radius for layer in layers)


@dataclass(slots=True)
class LayeredField:
    """Every layer's field, the via sites joining them, and the pads' escapes."""

    stack: RoutingStack
    reference_clearance: float
    inflation: float = 0.0
    """How far every obstacle in this field was already grown.

    A field's free space is not physical copper -- it is copper grown by whatever
    the routes in it need to keep clear. So "is there room here for a via" is not
    the distance to the boundary against the via's full clearance: that clearance
    has already been paid once, in the inflation. Charging it twice is the
    difference between a via fitting in a 1.7 mm channel and the router deciding the
    board has nowhere to put one.
    """
    layers: dict[str, LayerField] = field(default_factory=dict)
    sites: list[ViaSite] = field(default_factory=list)
    sites_by_triangle: dict[tuple[str, int], list[int]] = field(default_factory=dict)
    _escapes: dict[tuple[str, str], tuple[tuple[Point, int], ...]] = field(
        default_factory=dict
    )

    # -- congestion bookkeeping ------------------------------------------------

    def add_usage(self, crossings: dict[str, list[int]], demand: float) -> None:
        """Charge a route's demand to every cut it crosses."""
        for layer, edges in crossings.items():
            field_ = self.layers.get(layer)
            if field_ is None:
                continue
            for edge in edges:
                field_.used[edge] += demand

    def remove_usage(self, crossings: dict[str, list[int]], demand: float) -> None:
        """Give it back. This is the whole reason congestion is a number."""
        for layer, edges in crossings.items():
            field_ = self.layers.get(layer)
            if field_ is None:
                continue
            for edge in edges:
                field_.used[edge] = max(0.0, field_.used[edge] - demand)

    def congested(self) -> dict[str, list[int]]:
        """Every over-subscribed cut, by layer."""
        return {
            name: over
            for name, field_ in sorted(self.layers.items())
            if (over := field_.over_subscribed())
        }

    def age(self, history_mm: float) -> int:
        """Charge over-subscribed cuts for staying over-subscribed. Returns how many.

        This is PathFinder's history term, and it is what resolves the second-order
        congestion two nets fall into when each looks free only because the other
        has just left. Charging the corridor itself, permanently, stops them
        swapping.
        """
        count = 0
        for field_ in self.layers.values():
            for edge in field_.over_subscribed():
                overuse = field_.used[edge] / max(field_.capacity[edge], 1e-9) - 1.0
                field_.history[edge] += history_mm * overuse
                count += 1
        return count

    # -- geometry --------------------------------------------------------------

    def escapes(
        self, environment: RoutingEnvironment, pad: str, layer: str
    ) -> tuple[tuple[Point, int], ...]:
        """Where a route can leave a pad, on one layer.

        A pad is an obstacle in the shared field -- it has to be, because every
        *other* net has to avoid it -- so its own centre is not in the free space
        and cannot be a search endpoint. The way out is to search from the ring of
        points just outside the pad's hull instead, and let the per-net stretcher
        make the last millimetre to the pad itself, which it can do because it
        excludes the net's own pads.

        Several escapes, not one: which side of a fine-pitch pad a route leaves on
        is exactly the decision this milestone exists to make well, and offering the
        search a single point would make it for it.
        """
        obstacle = environment.obstacles.get(pad)
        if obstacle is None:
            return ()
        return self.escapes_from(obstacle.polygon, layer, pad)

    def escapes_from(
        self, polygon: tuple[Point, ...], layer: str, key: str
    ) -> tuple[tuple[Point, int], ...]:
        """The same, for anything with an outline -- a pad, or a pair's two pads."""
        cached = self._escapes.get((key, layer))
        if cached is not None:
            return cached

        found: list[tuple[Point, int]] = []
        layer_field = self.layers.get(layer)
        if layer_field is not None and polygon:
            # Rings, widening. The first is snug against the pad and is what a route
            # would normally use. When copper laid earlier has closed that ring off
            # entirely, a wider one finds the first clear air beyond it -- which is
            # the difference between "this pad has no route" and "this pad has to
            # start a little further out".
            base = self.reference_clearance + ESCAPE_GAP
            for scale in ESCAPE_RINGS:
                seen: set[int] = set()
                for point in inflate(polygon, base * scale):
                    triangle = layer_field.triangulation.locate(point)
                    if triangle is None or triangle in seen:
                        continue
                    seen.add(triangle)
                    found.append((point, triangle))
                if found:
                    break
        result = tuple(sorted(found))
        self._escapes[(key, layer)] = result
        return result

    def room_needed(self, via_radius: float) -> float:
        """How much clear space a via of this radius needs *in this field*."""
        return max(0.0, via_radius - self.inflation)

    def sites_in(self, layer: str, triangle: int) -> list[int]:
        return self.sites_by_triangle.get((layer, triangle), [])


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def build_field(
    environment: RoutingEnvironment,
    stack: RoutingStack,
    *,
    reference_clearance: float = 0.2,
    reference_width: float = 0.25,
    via_radius: float = 0.35,
    layout: Layout | None = None,
    origin: Point = (0.0, 0.0),
    blocking_for: Callable[[str], list[Obstacle]] | None = None,
    capacity_offset: float | None = None,
    inflation: float | None = None,
) -> LayeredField:
    """Build one field per copper layer, and the via sites that join them.

    ``reference_clearance`` is the largest clearance any net class asks for. Every
    obstacle is inflated by it, so a corridor that exists in this field exists for
    every net -- which is what lets one shared field carry the congestion accounting
    for nets that have different rules.

    Layers that carry a plane are built too, even though nothing routes on them: a
    via barrel passes through them, and it has to clear whatever is there.

    ``blocking_for`` overrides how obstacles are inflated, which is what turns this
    shared field into a *private* one: pass one net's own rules and the field it
    builds is the free space that net will actually be tightened in, so a path found
    in it is realizable rather than merely plausible. That is what the repair pass
    uses when the shared field's approximation has let a connection down.
    """
    fields: dict[str, LayerField] = {}
    keepouts = keepout_obstacles(layout, origin)
    for layer in stack.copper:
        blocking = (
            blocking_for(layer)
            if blocking_for is not None
            else environment.blocking(
                frozenset(), layer, clearance=reference_clearance, track_width=0.0
            )
        )
        # Inflated like everything else, and by the same reasoning: what the field
        # measures is the space a *centre-line* may occupy. A keepout added raw
        # would make the shared field think a channel holds two tracks where the
        # per-net geometry, which does inflate it, has room for one.
        grown = reference_clearance if inflation is None else inflation
        blocking.extend(
            replace(o, polygon=inflate(o.polygon, grown))
            for o in keepouts
            if o.blocks(frozenset(), layer)
        )
        try:
            free = free_space(
                environment,
                blocking,
                edge_margin=environment.edge_clearance + reference_width / 2,
            )
            triangulation = triangulate_free(free)
        except FreeSpaceError:
            # A layer with no room at all is not an error -- it is a layer nothing
            # routes on. The nets that wanted it will say so themselves.
            continue
        midpoints = [
            ((d.a[0] + d.b[0]) / 2, (d.a[1] + d.b[1]) / 2)
            for d in triangulation.diagonals
        ]
        offset = (
            reference_clearance if capacity_offset is None else capacity_offset
        )
        capacity = [
            triangulation.gate_width(i) + offset
            for i in range(len(triangulation.diagonals))
        ]
        fields[layer] = LayerField(
            layer=layer,
            free=free,
            triangulation=triangulation,
            capacity=capacity,
            used=[0.0] * len(capacity),
            history=[0.0] * len(capacity),
            midpoints=midpoints,
        )

    grown = reference_clearance if inflation is None else inflation
    sites, by_triangle = _via_sites(fields, max(0.0, via_radius - grown))
    return LayeredField(
        stack=stack,
        reference_clearance=reference_clearance,
        inflation=grown,
        layers=fields,
        sites=sites,
        sites_by_triangle=by_triangle,
    )


def keepout_obstacles(layout: Layout | None, origin: Point) -> list[Obstacle]:
    """Keepout regions, as obstacles on the layers they name.

    A keepout that names no layer applies to all of them, which is how a mounting
    hole behaves.
    """
    if layout is None:
        return []
    out: list[Obstacle] = []
    for index, keepout in enumerate(layout.placement.keepouts):
        if not keepout.tracks and not keepout.vias:
            continue
        out.append(_keepout_obstacle(keepout, index, origin))
    return out


def _keepout_obstacle(keepout: Keepout, index: int, origin: Point) -> Obstacle:
    x1, y1, x2, y2 = keepout.region_mm
    ox, oy = origin
    corners = (
        (ox + min(x1, x2), oy + min(y1, y2)),
        (ox + max(x1, x2), oy + min(y1, y2)),
        (ox + max(x1, x2), oy + max(y1, y2)),
        (ox + min(x1, x2), oy + max(y1, y2)),
    )
    return Obstacle(
        name=f"keepout:{index}",
        polygon=corners,
        net=None,
        layers=frozenset(keepout.layers),
        kind="keepout",
    )


def _via_sites(
    fields: dict[str, LayerField], via_radius: float
) -> tuple[list[ViaSite], dict[tuple[str, int], list[int]]]:
    """Where a via could go, as a discrete set.

    Candidates come from two places. Triangle incentres put a site in the middle of
    every pocket of free space, which is where a via wanting to escape a fine-pitch
    part has to land. A coarse grid fills the open areas, where the triangulation's
    few enormous triangles would otherwise offer one site for half the board.

    Continuous via positions are deliberately not optimised here -- the brief rules
    it out, and it would buy nothing: the stretcher moves the via to its equilibrium
    position afterwards, so the search only has to pick the right *pocket*.
    """
    if not fields:
        return [], {}

    candidates: list[Point] = []
    for layer_field in fields.values():
        for triangle in layer_field.triangulation.triangles:
            centre = _incentre(triangle)
            if centre is not None:
                candidates.append(centre)
    candidates.extend(_grid_points(fields, via_radius))

    sites: list[ViaSite] = []
    by_triangle: dict[tuple[str, int], list[int]] = {}
    seen: set[Point] = set()
    # Sorted and de-duplicated on a grid finer than a via, so the site list is a
    # function of the board and not of the order the layers happened to be built.
    for point in sorted({_quantise(p) for p in candidates}):
        if point in seen:
            continue
        seen.add(point)
        room: dict[str, float] = {}
        homes: dict[str, int] = {}
        for name, layer_field in fields.items():
            shapely_point = ShapelyPoint(point)
            if not layer_field.free.covers(shapely_point):
                continue
            located = layer_field.triangulation.locate(point)
            if located is None:
                continue
            room[name] = float(layer_field.free.boundary.distance(shapely_point))
            homes[name] = located
        if not room or max(room.values()) < via_radius:
            continue
        index = len(sites)
        sites.append(ViaSite(index=index, point=point, room=room, triangle=homes))
        for name, located in homes.items():
            by_triangle.setdefault((name, located), []).append(index)

    # One triangle can hold dozens of near-identical candidates, and the search
    # would consider every one of them from every gate -- for a choice the stretcher
    # is going to refine away anyway. Keeping the roomiest few per triangle costs
    # nothing in quality and is the difference between a board routing in a second
    # and in half a minute.
    for cut, found in by_triangle.items():
        if len(found) > MAX_SITES_PER_TRIANGLE:
            layer = cut[0]
            by_triangle[cut] = sorted(
                sorted(found, key=lambda i: (-sites[i].room.get(layer, 0.0), i))[
                    :MAX_SITES_PER_TRIANGLE
                ]
            )
    return sites, by_triangle


def _grid_points(fields: dict[str, LayerField], via_radius: float) -> list[Point]:
    """A coarse lattice over the board, at roughly twice the via pitch."""
    bounds = [f.free.bounds for f in fields.values()]
    if not bounds:
        return []
    min_x = min(b[0] for b in bounds)
    min_y = min(b[1] for b in bounds)
    max_x = max(b[2] for b in bounds)
    max_y = max(b[3] for b in bounds)
    step = max(VIA_SITE_SPACING * 2 * via_radius, 0.5)
    points: list[Point] = []
    steps_x = int((max_x - min_x) / step) + 1
    steps_y = int((max_y - min_y) / step) + 1
    if steps_x * steps_y > 20000:  # pragma: no cover - a metre-square board
        return []
    for i in range(steps_x + 1):
        for j in range(steps_y + 1):
            points.append((min_x + i * step, min_y + j * step))
    return points


def _incentre(triangle: tuple[Point, Point, Point]) -> Point | None:
    """The centre of a triangle's inscribed circle -- the roomiest point in it."""
    (ax, ay), (bx, by), (cx, cy) = triangle
    a = math.dist((bx, by), (cx, cy))
    b = math.dist((ax, ay), (cx, cy))
    c = math.dist((ax, ay), (bx, by))
    perimeter = a + b + c
    if perimeter <= 1e-9:
        return None
    return (
        (a * ax + b * bx + c * cx) / perimeter,
        (a * ay + b * by + c * cy) / perimeter,
    )


def _quantise(point: Point, grid: float = 0.05) -> Point:
    return (round(point[0] / grid) * grid, round(point[1] / grid) * grid)

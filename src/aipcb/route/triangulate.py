"""Triangulating the free space, and expressing paths as crossing sequences.

This is the machinery ADR 0006 settled on, following the shortest-homotopic-path
literature: triangulate the routing area, describe a path's homotopy class by the
sequence of triangulation diagonals it crosses, and reduce that sequence to a
canonical form.

The constrained Delaunay triangulation comes from Shapely (GEOS), rather than being
written here -- the brief asks for well-understood computational geometry over
invented algorithms, and a hand-rolled CDT is a classic source of subtle,
data-dependent bugs.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from itertools import pairwise

from shapely import Polygon as ShapelyPolygon
from shapely import constrained_delaunay_triangles
from shapely.geometry import Point as ShapelyPoint
from shapely.ops import unary_union

from aipcb.route.obstacles import Obstacle, RoutingEnvironment

__all__ = [
    "Diagonal",
    "FreeSpaceError",
    "Triangulation",
    "build_triangulation",
    "reduce_crossings",
]

Point = tuple[float, float]

#: A gate this many times the required corridor width counts as open space and
#: costs nothing extra. Below it, squeezing through is charged for.
_ROOMY_MULTIPLE = 4.0

#: Coordinates are snapped to this grid before triangulating, so that two vertices
#: that differ only by floating-point noise become the same vertex. Nanometres --
#: KiCad's own internal resolution.
_QUANT = 1e-6


class FreeSpaceError(ValueError):
    """The routing area is empty or a required point lies outside it."""


def _key(point: Point) -> Point:
    return (round(point[0] / _QUANT) * _QUANT, round(point[1] / _QUANT) * _QUANT)


@dataclass(frozen=True, slots=True)
class Diagonal:
    """An interior edge of the triangulation -- the symbols of a crossing sequence."""

    index: int
    a: Point
    b: Point
    triangles: tuple[int, int]

    def other(self, triangle: int) -> int:
        return self.triangles[1] if self.triangles[0] == triangle else self.triangles[0]


@dataclass(slots=True)
class Triangulation:
    """A constrained Delaunay triangulation of the routable area."""

    triangles: list[tuple[Point, Point, Point]] = field(default_factory=list)
    diagonals: list[Diagonal] = field(default_factory=list)
    by_triangle: dict[int, list[int]] = field(default_factory=dict)
    """Triangle index to the indices of its interior edges."""

    def locate(self, point: Point) -> int | None:
        """Which triangle contains a point. ``None`` if it is outside the free area."""
        for index, triangle in enumerate(self.triangles):
            if _inside(point, triangle):
                return index
        return None

    def nearest(self, point: Point) -> int:
        """The triangle whose centroid is closest -- a fallback for points on an edge."""
        best = min(
            range(len(self.triangles)),
            key=lambda i: _distance_sq(point, _centroid(self.triangles[i])),
        )
        return best

    def gate_width(self, edge: int) -> float:
        """How wide the corridor is at a diagonal.

        This is the routing resource. Free space is what obstacles and already-laid
        tracks leave behind, so a gate that has been used by earlier routes is
        literally narrower on the next net's triangulation -- congestion needs no
        separate bookkeeping, it is visible in the geometry.
        """
        diagonal = self.diagonals[edge]
        return _distance(diagonal.a, diagonal.b)

    def portal_path(
        self,
        start: Point,
        start_triangle: int,
        goal: Point,
        goal_triangle: int,
        *,
        corridor: float = 0.0,
        congestion: float = 0.0,
    ) -> list[int]:
        """The diagonals a shortest route crosses, found by A* over portal midpoints.

        Two details matter, and both were learned the hard way.

        *Search across gates, not centroids.* A constrained Delaunay triangulation
        of sparse obstacles contains a few enormous triangles, and the distance
        between their centroids says almost nothing about how far a wire travels
        through them -- searching centroids sent a route to the board corner and
        back. Midpoint-to-midpoint across the gates a path actually passes through
        tracks the real corridor.

        *A node is a crossing, not an edge.* A diagonal can be entered from either
        of its two triangles, and which one you came from decides where you can go
        next. Keying the search on the diagonal alone lets it step through a
        diagonal without crossing it, which produces a sequence that is a valid walk
        on paper and a violent zigzag on the board. The node is therefore the pair
        (diagonal, triangle being entered).

        ``corridor`` and ``congestion`` add a cost for squeezing through narrow
        gates. Length alone is a poor objective for a board: the shortest route
        happily takes the one gap a later net had no alternative to, and the later
        net then fails. Charging for narrowness spends open space first and saves
        tight gaps for the routes that must use them.

        The returned sequence is already a crossing sequence, so no separate
        conversion step is needed.
        """
        if start_triangle == goal_triangle:
            return []

        roomy = corridor * _ROOMY_MULTIPLE

        def penalty(edge: int) -> float:
            if congestion <= 0 or corridor <= 0:
                return 0.0
            width = self.gate_width(edge)
            if width >= roomy:
                return 0.0
            return congestion * (roomy - width)

        midpoints = [
            ((d.a[0] + d.b[0]) / 2, (d.a[1] + d.b[1]) / 2) for d in self.diagonals
        ]

        Node = tuple[int, int]
        best: dict[Node, float] = {}
        previous: dict[Node, Node] = {}
        queue: list[tuple[float, float, Node]] = []

        for edge in self.by_triangle.get(start_triangle, ()):
            node = (edge, self.diagonals[edge].other(start_triangle))
            cost = _distance(start, midpoints[edge]) + penalty(edge)
            best[node] = cost
            heapq.heappush(
                queue, (cost + _distance(midpoints[edge], goal), cost, node)
            )

        final: Node | None = None
        while queue:
            _, cost, node = heapq.heappop(queue)
            if cost > best.get(node, float("inf")) + 1e-12:
                continue
            edge, triangle = node
            if triangle == goal_triangle:
                final = node
                break
            for neighbour in self.by_triangle.get(triangle, ()):
                if neighbour == edge:
                    continue
                candidate = (
                    cost
                    + _distance(midpoints[edge], midpoints[neighbour])
                    + penalty(neighbour)
                )
                onward = (neighbour, self.diagonals[neighbour].other(triangle))
                if candidate < best.get(onward, float("inf")) - 1e-12:
                    best[onward] = candidate
                    previous[onward] = node
                    heapq.heappush(
                        queue,
                        (candidate + _distance(midpoints[neighbour], goal), candidate, onward),
                    )

        if final is None:
            return []
        sequence: list[int] = []
        cursor: Node | None = final
        while cursor is not None:
            sequence.append(cursor[0])
            cursor = previous.get(cursor)
        return sequence[::-1]

    def crossings(self, triangle_path: list[int]) -> list[int]:
        """The diagonals crossed by a walk through triangles."""
        sequence: list[int] = []
        for current, following in pairwise(triangle_path):
            shared = self._shared(current, following)
            if shared is None:
                return []
            sequence.append(shared)
        return sequence

    def _shared(self, a: int, b: int) -> int | None:
        for edge_index in self.by_triangle.get(a, ()):
            if self.diagonals[edge_index].other(a) == b:
                return edge_index
        return None

    def sleeve(self, crossings: list[int], start: int) -> list[int]:
        """The triangles a reduced crossing sequence passes through."""
        triangles = [start]
        for edge_index in crossings:
            triangles.append(self.diagonals[edge_index].other(triangles[-1]))
        return triangles


def reduce_crossings(sequence: list[int]) -> list[int]:
    """Cancel adjacent repeats, giving a canonical form for the homotopy class.

    Crossing the same diagonal twice in a row means stepping over it and straight
    back, which no shortest path does. Left-greedy cancellation with a stack removes
    every such pair in one linear pass.
    """
    stack: list[int] = []
    for symbol in sequence:
        if stack and stack[-1] == symbol:
            stack.pop()
        else:
            stack.append(symbol)
    return stack


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def build_triangulation(
    environment: RoutingEnvironment,
    obstacles: list[Obstacle],
    *,
    edge_margin: float = 0.0,
) -> Triangulation:
    """Triangulate the board area with ``obstacles`` removed.

    The obstacles are already inflated by the routing clearance, so the free space
    this produces is exactly the region a wire's centre-line may occupy -- which is
    what makes a shortest path through it automatically a legal one.

    ``edge_margin`` erodes the board outline by the same reasoning: copper too close
    to the board edge is a DRC error, so the edge is pulled in before routing rather
    than the result being checked afterwards.
    """
    if len(environment.outline) < 3:
        raise FreeSpaceError(
            "the board has no usable outline, so there is nowhere to route; "
            "declare `layout.outline` or draw an edge in KiCad"
        )

    board = ShapelyPolygon(environment.outline)
    if edge_margin > 0:
        board = board.buffer(-edge_margin, join_style="mitre")
        if board.is_empty:
            raise FreeSpaceError(
                f"the board is smaller than the {edge_margin:.2f} mm of edge "
                "clearance copper needs; nothing can be routed"
            )
        if board.geom_type != "Polygon":
            board = max(board.geoms, key=lambda g: g.area)
    holes = [ShapelyPolygon(o.polygon) for o in obstacles if len(o.polygon) >= 3]
    free = board.difference(unary_union(holes)) if holes else board
    if free.is_empty:
        raise FreeSpaceError("obstacles cover the whole board; nothing can be routed")

    collection = constrained_delaunay_triangles(free)
    triangles: list[tuple[Point, Point, Point]] = []
    for geometry in getattr(collection, "geoms", [collection]):
        if geometry.is_empty or geometry.area <= 0:
            continue
        coords = [_key(c) for c in geometry.exterior.coords[:3]]
        if len(set(coords)) == 3:
            triangles.append((coords[0], coords[1], coords[2]))

    if not triangles:
        raise FreeSpaceError("the free area could not be triangulated")

    return _link(triangles)


def _link(triangles: list[tuple[Point, Point, Point]]) -> Triangulation:
    """Find which triangles share which edges."""
    shared: dict[tuple[Point, Point], list[int]] = {}
    for index, triangle in enumerate(triangles):
        sides = (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        )
        for a, b in sides:
            shared.setdefault((a, b) if a <= b else (b, a), []).append(index)

    triangulation = Triangulation(triangles=triangles)
    for (a, b), owners in sorted(shared.items()):
        if len(owners) != 2:
            continue  # a boundary edge is not a diagonal: nothing crosses it
        index = len(triangulation.diagonals)
        triangulation.diagonals.append(Diagonal(index, a, b, (owners[0], owners[1])))
        triangulation.by_triangle.setdefault(owners[0], []).append(index)
        triangulation.by_triangle.setdefault(owners[1], []).append(index)
    return triangulation


# ---------------------------------------------------------------------------
# small geometry
# ---------------------------------------------------------------------------


def _centroid(triangle: tuple[Point, Point, Point]) -> Point:
    return (
        (triangle[0][0] + triangle[1][0] + triangle[2][0]) / 3,
        (triangle[0][1] + triangle[1][1] + triangle[2][1]) / 3,
    )


def _distance_sq(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _distance(a: Point, b: Point) -> float:
    return float(_distance_sq(a, b) ** 0.5)


def _sign(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _inside(point: Point, triangle: tuple[Point, Point, Point]) -> bool:
    d1 = _sign(triangle[0], triangle[1], point)
    d2 = _sign(triangle[1], triangle[2], point)
    d3 = _sign(triangle[2], triangle[0], point)
    has_negative = min(d1, d2, d3) < -1e-12
    has_positive = max(d1, d2, d3) > 1e-12
    return not (has_negative and has_positive)


def contains_point(free: ShapelyPolygon, point: Point) -> bool:
    return bool(free.covers(ShapelyPoint(point)))

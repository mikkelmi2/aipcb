# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
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
from typing import Any

from shapely import Polygon as ShapelyPolygon
from shapely import constrained_delaunay_triangles
from shapely.geometry import Point as ShapelyPoint
from shapely.ops import unary_union

from aipcb.route.obstacles import Obstacle, RoutingEnvironment

__all__ = [
    "Diagonal",
    "FreeSpaceError",
    "SpecialCut",
    "Triangulation",
    "build_triangulation",
    "free_space",
    "reduce_crossings",
    "special_cuts",
    "triangulate_free",
]

Point = tuple[float, float]

#: A gate this many times the required corridor width counts as open space and
#: costs nothing extra. Below it, squeezing through is charged for.
_ROOMY_MULTIPLE = 4.0

#: Coordinates are snapped to this grid before triangulating, so that two vertices
#: that differ only by floating-point noise become the same vertex. Nanometres --
#: KiCad's own internal resolution.
_QUANT = 1e-6

#: How far off the line joining two apexes a shared diagonal's endpoint has to fall
#: before the quadrilateral counts as convex. A picometre: three orders below the
#: coordinate quantum, so noise never decides, and small enough that a quad that is
#: genuinely convex by a nanometre is still charged.
_CONVEX_TOL = 1e-9


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


@dataclass(frozen=True, slots=True)
class SpecialCut:
    """The other diagonal of two adjacent triangles -- a cut the CDT never drew.

    Maley's realizability criterion quantifies over *every* cut across the free
    space. A triangulation's own diagonals are only some of them, and the ones it
    misses are not exotic: take two triangles sharing a diagonal ``e``, and the
    segment joining their two far vertices is a second diagonal of the same
    quadrilateral. A wire that enters one of the triangles through its other two
    edges never touches ``e`` at all -- it rounds the apex instead -- but it does
    cross this segment, and the room it has to round the apex is this segment's
    length rather than ``e``'s.

    The constrained Delaunay triangulation chose ``e`` over this one, which is
    exactly the case where this one is the shorter and therefore the binding
    constraint. Measured on the bundled corpus at this milestone: between 11% and
    29% of diagonals per layer have a shorter partner, the shortest at 2.6% of the
    diagonal it pairs with. This is not a rare geometry.

    gEDA PCB's toporouter called these *special cuts* and rewrote its congestion
    accounting around them in 2009; see the
    :doc:`postmortem </notes/toporouter-postmortem>` §A.6 and ADR 0014. Derived
    here from that note and from Maley, never from the GPL-2.0 source it studied.
    """

    index: int
    diagonal: int
    """The CDT diagonal this one pairs with -- the other diagonal of the same quad."""
    a: Point
    b: Point
    """The two apexes: the far vertex of each of the diagonal's two triangles."""

    def length(self) -> float:
        return _distance(self.a, self.b)


@dataclass(slots=True)
class Triangulation:
    """A constrained Delaunay triangulation of the routable area."""

    triangles: list[tuple[Point, Point, Point]] = field(default_factory=list)
    diagonals: list[Diagonal] = field(default_factory=list)
    by_triangle: dict[int, list[int]] = field(default_factory=dict)
    """Triangle index to the indices of its interior edges."""
    _tree: object | None = None
    """A lazily built R-tree over the triangles, so ``locate`` is not a linear scan."""
    _components: list[int] | None = None
    """Lazily computed connected components of the free space."""
    _special: list[SpecialCut] | None = None
    """Lazily derived second diagonals. See :func:`special_cuts`."""

    def locate(self, point: Point) -> int | None:
        """Which triangle contains a point. ``None`` if it is outside the free area.

        Bounding boxes first. Multilayer routing locates points by the thousand --
        every candidate via site on every layer -- and a linear scan over the
        triangles turned that into most of the router's running time.
        """
        for index in sorted(self._candidates(point)):
            if _inside(point, self.triangles[index]):
                return index
        return None

    def locate_many(self, points: list[Point]) -> list[int | None]:
        """:meth:`locate`, for a whole list of points, in one tree query.

        Same answer, one call. The scalar version pays a Python-level Shapely call
        and a NumPy round trip *per point*, and the field builder locates points by
        the tens of thousands -- every candidate via site on every layer, once per
        field, and a field is rebuilt for every connection the shared one could not
        place. Deriving those sites was 54.7 s of an 87 s profiled run of
        `examples/pcie-sata`, and this query was most of it (M17c).

        The result is identical to calling :meth:`locate` on each point, including
        the tie-break: candidates are still tried in ascending triangle index, so
        a point on a shared edge still lands in the lower-numbered triangle.
        """
        if not points:
            return []
        import numpy as np
        from shapely import points as shapely_points

        tree = self._strtree()
        found = tree.query(shapely_points(np.array(points, dtype="float64")))
        buckets: dict[int, list[int]] = {}
        for position, triangle in zip(found[0].tolist(), found[1].tolist(), strict=True):
            buckets.setdefault(int(position), []).append(int(triangle))
        located: list[int | None] = []
        for position, point in enumerate(points):
            hit: int | None = None
            for index in sorted(buckets.get(position, ())):
                if _inside(point, self.triangles[index]):
                    hit = index
                    break
            located.append(hit)
        return located

    def _strtree(self) -> Any:
        """The R-tree over the triangles, built once and in one Shapely call.

        ``shapely.polygons`` on a single array rather than a list comprehension of
        ``Polygon(t)``: a large board's triangulation runs to three thousand
        triangles and a field is rebuilt per repaired connection, so the difference
        is a quarter of a million Python-level constructor calls (M17c).
        """
        if self._tree is None:
            import numpy as np
            from shapely import STRtree, polygons

            self._tree = STRtree(
                polygons(np.array(self.triangles, dtype="float64"))
                if self.triangles
                else []
            )
        return self._tree

    def _candidates(self, point: Point) -> list[int]:
        return [int(i) for i in self._strtree().query(ShapelyPoint(point))]

    def component(self, triangle: int) -> int:
        """Which connected piece of the free space a triangle belongs to.

        Inflating obstacles by a clearance does not merely narrow the free space --
        it breaks it into pieces, and the pieces are what decide whether two points
        can be joined at all. Knowing this before searching turns "the search found
        nothing" into "these are on opposite sides of a wall", which is both a
        better message and a cheaper answer.
        """
        if self._components is None:
            parent = list(range(len(self.triangles)))

            def find(i: int) -> int:
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for diagonal in self.diagonals:
                a, b = find(diagonal.triangles[0]), find(diagonal.triangles[1])
                if a != b:
                    parent[min(a, b)] = max(a, b)
            self._components = [find(i) for i in range(len(self.triangles))]
        return self._components[triangle]

    def nearest(self, point: Point) -> int:
        """The triangle whose centroid is closest -- a fallback for points on an edge."""
        best = min(
            range(len(self.triangles)),
            key=lambda i: _distance_sq(point, _centroid(self.triangles[i])),
        )
        return best

    def special_cuts(self) -> list[SpecialCut]:
        """The second diagonal of every convex adjacent triangle pair, derived once."""
        if self._special is None:
            self._special = special_cuts(self)
        return self._special

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


def special_cuts(triangulation: Triangulation) -> list[SpecialCut]:
    """Derive the second diagonal of every adjacent triangle pair.

    One pass over the diagonals. For each, take the far vertex of each of its two
    triangles -- ``opv`` and ``opv2`` in the postmortem's diagram -- and join them.

    **Only convex quadrilaterals qualify**, and the test is what keeps this sound.
    Two triangles sharing a diagonal form a quad that may be re-entrant; a CDT is
    free to leave such a pair unflipped precisely because flipping it would put the
    new diagonal *outside* the free space. A segment that leaves the free area is
    not a cut across it -- charging wires to it would invent congestion where there
    is none.

    Convexity is four orientation tests and no geometry library: the two apexes
    must fall on opposite sides of the shared diagonal, and the shared diagonal's
    two ends must fall on opposite sides of the apex-to-apex line. The first is
    already true of any pair of triangles that share an edge without overlapping,
    so in a well-formed triangulation it never fires -- it is checked anyway,
    because a function that is correct only when its caller behaves is a function
    whose bugs land somewhere else.

    The result is deterministic: diagonals are visited in index order and every cut
    is a function of the triangulation alone.
    """
    cuts: list[SpecialCut] = []
    for diagonal in triangulation.diagonals:
        first, second = diagonal.triangles
        a = _apex(triangulation.triangles[first], diagonal)
        b = _apex(triangulation.triangles[second], diagonal)
        if a is None or b is None or a == b:
            continue
        span = _distance(a, b)
        if span <= 0:
            continue
        # Signed perpendicular distances, so the tolerance means millimetres rather
        # than millimetres-squared and does not change meaning when the board sits
        # far from the origin.
        if not _straddles(a, b, diagonal.a, diagonal.b, span):
            continue
        edge = _distance(diagonal.a, diagonal.b)
        if edge <= 0 or not _straddles(diagonal.a, diagonal.b, a, b, edge):
            continue
        cuts.append(SpecialCut(index=len(cuts), diagonal=diagonal.index, a=a, b=b))
    return cuts


def _straddles(a: Point, b: Point, first: Point, second: Point, span: float) -> bool:
    """Whether ``first`` and ``second`` lie on opposite sides of the line ``a``-``b``."""
    left = _sign(a, b, first) / span
    right = _sign(a, b, second) / span
    return min(left, right) < -_CONVEX_TOL and max(left, right) > _CONVEX_TOL


def _apex(triangle: tuple[Point, Point, Point], diagonal: Diagonal) -> Point | None:
    """The vertex of a triangle that is not an end of one of its diagonals."""
    for vertex in triangle:
        if vertex != diagonal.a and vertex != diagonal.b:
            return vertex
    return None


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


def free_space(
    environment: RoutingEnvironment,
    obstacles: list[Obstacle],
    *,
    edge_margin: float = 0.0,
) -> ShapelyPolygon:
    """The region a wire's centre-line may occupy, as one geometry.

    Split out from :func:`build_triangulation` because the multilayer router needs
    the polygon as well as the triangulation: deciding whether a via fits somewhere
    is a distance-to-the-nearest-obstacle question, which a triangulation answers
    badly and a polygon answers exactly.
    """
    if len(environment.outline) < 3:
        raise FreeSpaceError(
            "the board has no usable outline, so there is nowhere to route; "
            "declare `layout.outline` or draw an edge in KiCad"
        )

    # Cutouts are holes in the shell, not obstacles removed from it. Negative
    # buffering a polygon with holes shrinks the shell and *grows* the holes, which
    # is exactly the edge clearance a milled window needs -- and it means a slot
    # that separates two corridors separates them in the triangulation too, so the
    # homotopy model can tell going round it one way from going round it the other.
    board = ShapelyPolygon(environment.outline, environment.cutouts)
    if edge_margin > 0:
        board = board.buffer(-edge_margin, join_style="mitre")
        if board.is_empty:
            raise FreeSpaceError(
                f"the board is smaller than the {edge_margin:.2f} mm of edge "
                "clearance copper needs; nothing can be routed"
            )
        if board.geom_type == "MultiPolygon" and len(board.geoms) > 1:
            # A cutout that reaches across the board splits it into pieces. Routing
            # in the biggest one is the only honest answer: copper cannot jump a
            # slot, and the nets that needed to are about to say so themselves.
            board = max(board.geoms, key=lambda g: g.area)
        if board.geom_type != "Polygon":
            board = max(board.geoms, key=lambda g: g.area)
    holes = [ShapelyPolygon(o.polygon) for o in obstacles if len(o.polygon) >= 3]
    free = board.difference(unary_union(holes)) if holes else board
    if free.is_empty:
        raise FreeSpaceError("obstacles cover the whole board; nothing can be routed")
    return free


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
    return triangulate_free(
        free_space(environment, obstacles, edge_margin=edge_margin)
    )


def triangulate_free(free: ShapelyPolygon) -> Triangulation:
    """Triangulate a free-space geometry that has already been computed."""
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

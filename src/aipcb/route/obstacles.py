# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Extracting the routing environment from a generated board.

The stretcher needs to know what a wire must go around: pads belonging to other
nets, the board edge, and anything else that occupies copper. Those come from the
board itself rather than from the source, because the board is where placement has
actually been resolved -- including any hand-placement that M6 preserved.

Every obstacle is a convex polygon **inflated by the clearance the route needs**.
That is the trick that makes the whole approach work: tighten a wire against
inflated hulls and the shortest path is automatically a legal one, rather than
something that has to be checked and patched afterwards.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import lru_cache

from aipcb.compile.geometry import rotate_kicad
from aipcb.kicad.sexpr import SNode
from aipcb.model.board import Arc, arc_centre

__all__ = [
    "EDGE_CLEARANCE",
    "Clearances",
    "Obstacle",
    "Polygon",
    "RoutingEnvironment",
    "arc_slack",
    "board_outline",
    "board_rings",
    "extract_obstacles",
    "inflate",
    "preserved_copper",
]

Point = tuple[float, float]
Polygon = tuple[Point, ...]
#: Looks up a net's clearance, so the larger of two nets' rules can be honoured.
Clearances = Callable[[str | None], float]

#: KiCad's default board-edge clearance. Copper closer than this to the outline is
#: a DRC error, so the routable area is eroded by it before anything is routed.
EDGE_CLEARANCE = 0.5

#: How many segments approximate a circular pad or an inflated corner. Eight is
#: enough that the error is well under a fabrication tolerance, and few enough to
#: keep the triangulation small.
_ARC_SEGMENTS = 8


@dataclass(frozen=True, slots=True)
class Obstacle:
    """Something a route must not cross."""

    name: str
    """``U1.7`` for a pad, ``U1`` for a body, ``via:v1`` for a via."""
    polygon: Polygon
    net: str | None = None
    layers: frozenset[str] = frozenset()
    kind: str = "pad"

    def blocks(self, net: str | frozenset[str], layer: str) -> bool:
        """Whether this obstacle is in the way of a route on ``net`` and ``layer``.

        A component body does not block copper. A courtyard states how much room a
        part needs on the board, not that its footprint is a copper keep-out --
        running a track under an SMD chip is normal and legal. Bodies are extracted
        anyway so that a sketch can name one as something to pass, which is often
        how a person would describe the route.
        """
        if self.kind == "body":
            return False
        own = {net} if isinstance(net, str) else net
        if self.net is not None and self.net in own:
            return False
        return not self.layers or layer in self.layers or "*.Cu" in self.layers

    def centroid(self) -> Point:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return (sum(xs) / len(xs), sum(ys) / len(ys))


@dataclass(slots=True)
class RoutingEnvironment:
    """Everything the stretcher needs to know about one board.

    Obstacle polygons here are *physical* copper, not inflated. How much room a
    route must leave depends on that route's own width and on the larger of the two
    nets' clearances, neither of which is known when the board is read -- so
    inflation happens at the point of use, in :meth:`blocking`.
    """

    outline: Polygon
    cutouts: tuple[Polygon, ...] = ()
    """Holes through the board. They pierce every layer, and they separate paths.

    A cutout is not an obstacle in the ``obstacles`` dict, because it is not copper
    and no net owns it -- it is a hole in the free space itself, and that is where
    the topology model wants it: two points either side of a slot are in different
    homotopy classes, and going round it clockwise is a different route from going
    round it the other way. Feeding it in as a hole is what makes that true rather
    than merely stated.
    """
    edge_clearance: float = EDGE_CLEARANCE
    """How far copper must stay from the outline and from every cutout.

    This is the source's ``edge_clearance`` **plus** :attr:`arc_slack`, because it
    is measured against the polygon this environment holds rather than against the
    curve that polygon approximates.
    """
    arc_slack: float = 0.0
    """How far this outline's chords cut inside the arcs they stand in for.

    An ``Edge.Cuts`` arc is approximated by chords, and a chord runs *inside* its
    arc by the sagitta -- 8 um on the 0.95 mm keying notch of a PCIe card edge, at
    the 24-chords-per-circle this toolchain samples at. Where the board is on the
    outside of the arc, as it is at a notch, that makes the free area larger than
    the board and the router spends the difference as clearance: measured on
    `examples/pcie-sata`, ten tracks came 0.0081 mm closer to the notch than the
    0.15 mm the source asked for, and KiCad's DRC said so. Adding the worst
    sagitta on the board to the edge keep-out costs microns of routable area and
    makes the approximation safe in both directions.
    """
    obstacles: dict[str, Obstacle] = field(default_factory=dict)
    pad_centres: dict[str, Point] = field(default_factory=dict)
    pad_nets: dict[str, str] = field(default_factory=dict)
    pad_layers: dict[str, frozenset[str]] = field(default_factory=dict)

    def resolve_pad(self, reference: str) -> str | None:
        """Map a pad reference such as ``U1.2`` to a concrete pad instance."""
        if reference in self.pad_centres:
            return reference
        prefix = f"{reference}#"
        candidates = sorted(k for k in self.pad_centres if k.startswith(prefix))
        return candidates[0] if candidates else None

    def blocking(
        self,
        net: str | frozenset[str],
        layer: str,
        *,
        clearance: float = 0.2,
        track_width: float = 0.25,
        clearance_of: Clearances | None = None,
        open_pads: frozenset[str] = frozenset(),
    ) -> list[Obstacle]:
        """Obstacles in the way of a route, inflated by the room it must leave.

        KiCad enforces the *larger* of the two nets' clearances between any two
        pieces of copper, so a route through a 0.2 mm-clearance net still has to
        keep 0.25 mm away from a 0.25 mm-clearance power pad. Using only the routing
        net's own figure produced violations short by exactly the difference.

        A net's *own* pads are a subtler case. Copper may legally overlap them --
        it is the same net -- but a track that clips one tangentially leaves a
        crescent of copper a few microns wide, which KiCad reports as a sliver and a
        fabricator would rather not etch. So a route treats its own net's other pads
        as things to go round, and only the two pads it is actually joining, named in
        ``open_pads``, are open to it. Copper of the same net that is *not* a pad --
        another track, a via -- never blocks: running alongside or across it is what
        a net does.
        """
        out: list[Obstacle] = []
        for _, obstacle in sorted(self.obstacles.items()):
            own = {net} if isinstance(net, str) else net
            mine = obstacle.net is not None and obstacle.net in own
            if mine and obstacle.kind == "pad" and obstacle.name not in open_pads:
                if not obstacle.layers or layer in obstacle.layers or "*.Cu" in obstacle.layers:
                    other = clearance_of(obstacle.net) if clearance_of else clearance
                    margin = max(clearance, other) + track_width / 2
                    out.append(
                        replace(obstacle, polygon=inflate(obstacle.polygon, margin))
                    )
                continue
            if not obstacle.blocks(net, layer):
                continue
            other = clearance_of(obstacle.net) if clearance_of else clearance
            margin = max(clearance, other) + track_width / 2
            out.append(replace(obstacle, polygon=inflate(obstacle.polygon, margin)))
        return out


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _rotate(point: Point, degrees: float) -> Point:
    """Rotate a point about the origin, KiCad's way round.

    KiCad's rotation is counter-clockwise *as drawn*, and its files have Y pointing
    down, so the transform is the mirror of the textbook one. Getting it backwards
    The transform itself lives in :func:`aipcb.compile.geometry.rotate_kicad`, so
    the board writer and the obstacle extractor cannot disagree about it.
    """
    return rotate_kicad(point, degrees)


def _rect(width: float, height: float) -> Polygon:
    half_w, half_h = width / 2, height / 2
    return ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h))


def _circle(radius: float, segments: int = _ARC_SEGMENTS) -> Polygon:
    # A circumscribed polygon, so the approximation never under-states the pad.
    scale = radius / math.cos(math.pi / segments)
    return tuple(
        (
            scale * math.cos(2 * math.pi * i / segments),
            scale * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments)
    )


def _stadium(width: float, height: float) -> Polygon:
    """An oval pad, as a convex hull of two circles."""
    radius = min(width, height) / 2
    offset = (max(width, height) - 2 * radius) / 2
    centres = (
        ((-offset, 0.0), (offset, 0.0))
        if width >= height
        else ((0.0, -offset), (0.0, offset))
    )
    points = [
        (cx + px, cy + py) for cx, cy in centres for px, py in _circle(radius)
    ]
    return convex_hull(tuple(points))


def convex_hull(points: tuple[Point, ...]) -> Polygon:
    """Andrew's monotone chain. Returns counter-clockwise, without duplicates."""
    unique = sorted(set(points))
    if len(unique) <= 2:
        return tuple(unique)

    def build(sequence: list[Point]) -> list[Point]:
        chain: list[Point] = []
        for point in sequence:
            while len(chain) >= 2 and _cross(chain[-2], chain[-1], point) <= 0:
                chain.pop()
            chain.append(point)
        return chain

    lower = build(unique)
    upper = build(unique[::-1])
    return tuple(lower[:-1] + upper[:-1])


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


#: How many inflated hulls are remembered. A board's obstacle set is inflated once
#: per net class per layer per route, and the same pad at the same margin comes out
#: the same every time -- so the second and later times are a dictionary lookup
#: rather than a Minkowski sum and a convex hull (M17c). Sized for a large board's
#: obstacle count times a handful of distinct margins; the cache is a speed device
#: and nothing depends on its contents surviving.
_INFLATE_CACHE = 1 << 16


@lru_cache(maxsize=_INFLATE_CACHE)
def _inflated(polygon: Polygon, margin: float) -> Polygon:
    disc = _circle(margin)
    return convex_hull(
        tuple((px + dx, py + dy) for px, py in polygon for dx, dy in disc)
    )


def inflate(polygon: Polygon, margin: float) -> Polygon:
    """Grow a convex polygon outward by ``margin`` in every direction.

    Implemented as the convex hull of small circles placed at each vertex -- the
    Minkowski sum of the polygon with a disc, which is exactly the set of points
    within ``margin`` of it. Approximating the disc by a circumscribed polygon keeps
    the result conservative: never smaller than the true offset, so a path that
    clears the inflated hull always clears the real one.

    Memoised, because it is a pure function of a hashable polygon and a margin and
    the router asks for the same answer thousands of times: 45 000 calls on
    `examples/pcie-sata`, of which fewer than a thousand are distinct.
    """
    if margin <= 0 or not polygon:
        return polygon
    return _inflated(polygon, margin)


def polygon_area(polygon: Polygon) -> float:
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return total / 2


# ---------------------------------------------------------------------------
# reading a board
# ---------------------------------------------------------------------------


def board_outline(board: SNode) -> Polygon:
    """The board's outer edge, as a closed polygon in KiCad coordinates."""
    return board_rings(board)[0]


def board_rings(board: SNode) -> tuple[Polygon, tuple[Polygon, ...]]:
    """Recover the board edge and its holes from the ``Edge.Cuts`` graphics.

    Before M9 this was the convex hull of every ``Edge.Cuts`` point, which is
    conservative for a rectangle and simply wrong for anything else: it fills in the
    missing corner of an L-shaped board and it cannot see a hole at all. So the
    graphics are chained back into the closed loops they are, the largest becomes
    the boundary, and every loop inside it becomes a hole.

    Reading the geometry rather than the source is deliberate and unchanged from M7:
    the board is where placement has actually been resolved, and a hand-drawn edge
    -- which is how most boards get one -- has no source to read.
    """
    loops: list[list[Point]] = []
    open_chains: list[list[Point]] = []
    for item in board.children():
        if item.get("layer") != "Edge.Cuts":
            continue
        piece = _edge_points(item)
        if len(piece) < 2:
            continue
        if _same(piece[0], piece[-1]):
            loops.append(piece[:-1])
        else:
            open_chains.append(piece)

    loops.extend(_chain(open_chains))
    rings = [tuple(loop) for loop in loops if len(loop) >= 3]
    if not rings:
        return (), ()
    return _classify(rings)


def _edge_points(item: SNode) -> list[Point]:
    """One ``Edge.Cuts`` graphic as a polyline."""
    def xy(token: str) -> Point | None:
        node = item.child(token)
        if node is None:
            return None
        return (float(node.value(0) or 0), float(node.value(1) or 0))

    if item.name in ("gr_arc", "arc"):
        start, mid, end = xy("start"), xy("mid"), xy("end")
        if start is None or mid is None or end is None:
            return []
        return list(Arc(start, mid, end).points())
    if item.name in ("gr_circle", "circle"):
        centre, edge = xy("center"), xy("end")
        if centre is None or edge is None:
            return []
        radius = math.dist(centre, edge)
        steps = 32
        ring = [
            (centre[0] + radius * math.cos(math.tau * i / steps),
             centre[1] + radius * math.sin(math.tau * i / steps))
            for i in range(steps)
        ]
        return [*ring, ring[0]]
    if item.name in ("gr_rect", "rect"):
        start, end = xy("start"), xy("end")
        if start is None or end is None:
            return []
        return [
            start, (end[0], start[1]), end, (start[0], end[1]), start,
        ]
    if item.name in ("gr_poly", "gr_curve", "poly"):
        pts = item.child("pts")
        if pts is None:
            return []
        points = [
            (float(node.value(0) or 0), float(node.value(1) or 0))
            for node in pts.children("xy")
        ]
        if len(points) >= 3 and not _same(points[0], points[-1]):
            points.append(points[0])
        return points
    start, end = xy("start"), xy("end")
    if start is None or end is None:
        return []
    return [start, end]


#: Two edge endpoints are the same corner if they agree to this many millimetres.
#: A thousandth of KiCad's own display resolution, and far tighter than any real
#: gap somebody would draw on purpose.
_JOIN_TOLERANCE = 1e-3


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= _JOIN_TOLERANCE and abs(a[1] - b[1]) <= _JOIN_TOLERANCE


def _chain(pieces: list[list[Point]]) -> list[list[Point]]:
    """Join open polylines end to end into whatever closed loops they form.

    A piece that never closes is dropped rather than guessed at: an unclosed edge is
    not a board, and inventing the missing segment would put copper outside a
    boundary the fabricator will actually cut.
    """
    remaining = list(pieces)
    loops: list[list[Point]] = []
    while remaining:
        chain = remaining.pop(0)
        progressed = True
        while progressed and not _same(chain[0], chain[-1]):
            progressed = False
            for index, piece in enumerate(remaining):
                if _same(chain[-1], piece[0]):
                    chain = chain + piece[1:]
                elif _same(chain[-1], piece[-1]):
                    chain = chain + piece[-2::-1]
                elif _same(chain[0], piece[-1]):
                    chain = piece[:-1] + chain
                elif _same(chain[0], piece[0]):
                    chain = piece[:0:-1] + chain
                else:
                    continue
                remaining.pop(index)
                progressed = True
                break
        if _same(chain[0], chain[-1]) and len(chain) >= 4:
            loops.append(chain[:-1])
    return loops


def _classify(rings: list[Polygon]) -> tuple[Polygon, tuple[Polygon, ...]]:
    """The biggest ring is the board; the ones inside it are its holes."""
    from shapely.geometry import Polygon as ShapelyPolygon

    shapes = [(ring, ShapelyPolygon(ring)) for ring in rings]
    shapes = [(ring, shape) for ring, shape in shapes if shape.is_valid and shape.area > 0]
    if not shapes:
        return convex_hull(tuple(p for ring in rings for p in ring)), ()
    outer_ring, outer = max(shapes, key=lambda pair: pair[1].area)
    holes = tuple(
        ring for ring, shape in shapes if shape is not outer and outer.contains(shape)
    )
    return outer_ring, holes


def _pad_polygon(pad: SNode, rotation: float) -> Polygon:
    """The pad's outline in board coordinates, before inflation."""
    at = pad.child("at")
    size = pad.child("size")
    if at is None or size is None:
        return ()
    px, py = float(at.value(0) or 0), float(at.value(1) or 0)
    pad_rotation = float(at.value(2) or 0)
    width, height = float(size.value(0) or 0), float(size.value(1) or 0)

    atoms = [a.value for a in pad.atoms()]
    shape = atoms[2] if len(atoms) > 2 else "rect"

    if shape == "circle":
        local = _circle(max(width, height) / 2)
    elif shape == "oval":
        local = _stadium(width, height)
    else:
        # rect, roundrect, trapezoid, custom: the bounding rectangle is a safe
        # over-approximation, and over-approximating an obstacle is never unsafe.
        local = _rect(width, height)

    # The pad's own angle is *absolute* in KiCad's format -- it already includes the
    # footprint's rotation -- so the shape is turned by it once. Only the pad's
    # position within the footprint is turned again, by the footprint. Turning the
    # whole thing by both spins every pad shape twice, which for an oval pad points
    # it across the part instead of along it.
    spun = tuple(_rotate(point, pad_rotation) for point in local)
    ox, oy = _rotate((px, py), rotation)
    return tuple((x + ox, y + oy) for x, y in spun)


def arc_slack(board: SNode) -> float:
    """The worst distance any ``Edge.Cuts`` chord cuts inside its own arc.

    Zero for a board whose edge is all straight lines, which is most of them.
    """
    worst = 0.0
    for item in board.children():
        if not isinstance(item, SNode) or item.name not in ("gr_arc", "arc"):
            continue
        if item.get("layer") != "Edge.Cuts":
            continue
        points = [item.child(t) for t in ("start", "mid", "end")]
        if any(p is None for p in points):
            continue
        a, mid, b = (
            (float(p.value(0) or 0), float(p.value(1) or 0))
            for p in points
            if p is not None
        )
        arc = Arc(a, mid, b)
        centre = arc_centre(a, mid, b)
        if centre is None:
            continue
        radius = math.dist(centre, a)
        chords = max(1, len(arc.points()) - 1)
        sweep = abs(_arc_sweep(centre, a, mid, b))
        worst = max(worst, radius * (1 - math.cos(sweep / chords / 2)))
    return round(worst, 6)


def _arc_sweep(centre: Point, a: Point, mid: Point, b: Point) -> float:
    """The angle the arc turns through, signed, the same way ``Arc.points`` reads it."""
    cx, cy = centre
    start = math.atan2(a[1] - cy, a[0] - cx)
    through = math.atan2(mid[1] - cy, mid[0] - cx)
    finish = math.atan2(b[1] - cy, b[0] - cx)
    first = (through - start) % math.tau
    second = (finish - through) % math.tau
    total = first + second
    return total if total <= math.tau else total - math.tau


def extract_obstacles(
    board: SNode, *, edge_clearance: float = EDGE_CLEARANCE
) -> RoutingEnvironment:
    """Build the routing environment from a board, in physical dimensions."""
    outline, cutouts = board_rings(board)
    slack = arc_slack(board)
    environment = RoutingEnvironment(
        outline=outline,
        cutouts=cutouts,
        edge_clearance=edge_clearance + slack,
        arc_slack=slack,
    )

    for footprint in board.children("footprint"):
        at = footprint.child("at")
        if at is None:
            continue
        fx, fy = float(at.value(0) or 0), float(at.value(1) or 0)
        rotation = float(at.value(2) or 0)
        refdes = _reference(footprint) or footprint.get("uuid") or "?"

        for pad in footprint.children("pad"):
            number = pad.value(0)
            if number is None:
                continue
            outline = _pad_polygon(pad, rotation)
            if not outline:
                continue
            absolute = tuple((x + fx, y + fy) for x, y in outline)
            net_node = pad.child("net")
            net = net_node.value(1) if net_node is not None else None
            layers = frozenset(a.value for a in (pad.child("layers") or SNode("x")).atoms())
            name = f"{refdes}.{number}"
            # Several pads legitimately share a number: a USB receptacle's four
            # shield tabs are all pad 6. Keying obstacles by name alone let each
            # overwrite the last, leaving three of them invisible -- and a track
            # ran straight through one, shorting two nets.
            key = name
            suffix = 1
            while key in environment.obstacles:
                suffix += 1
                key = f"{name}#{suffix}"
            environment.obstacles[key] = Obstacle(
                name=key,
                polygon=convex_hull(absolute),
                net=net,
                layers=layers,
                kind="pad",
            )
            # Keyed by the unique key, not the pad number. A SOT-223's pin 2 and
            # its thermal tab are both pad 2 and both on the output net, but they
            # are separate copper: KiCad reports them unconnected until a track
            # joins them, so the router has to see them as two things to connect.
            centre = _centroid(absolute)
            environment.pad_centres[key] = centre
            if net:
                environment.pad_nets[key] = net
            environment.pad_layers[key] = layers

        body = _courtyard(footprint, fx, fy, rotation)
        if body:
            environment.obstacles[refdes] = Obstacle(
                name=refdes, polygon=body, net=None, layers=frozenset(), kind="body"
            )

    return environment


def preserved_copper(board: SNode) -> list[Obstacle]:
    """Copper already on the board, as fixed obstacles.

    Someone routed a differential pair by hand, or poured half a supply rail, and
    M6's incremental build kept it. The router has to go *around* that, not through
    it -- so it is read back out of the board and turned into obstacles exactly as a
    pad is. The nets are resolved to names rather than left as KiCad's numeric codes,
    because a route has to be able to recognise its own copper and pass through it.
    """
    names = {
        code: name
        for node in board.children("net")
        if (code := node.value(0)) is not None and (name := node.value(1)) is not None
    }

    def net_of(item: SNode) -> str | None:
        code = item.get("net")
        return names.get(code) if code is not None else None

    obstacles: list[Obstacle] = []
    for index, item in enumerate(board.children("segment")):
        start, end = item.child("start"), item.child("end")
        width = item.child("width")
        layer = item.get("layer")
        if start is None or end is None or layer is None:
            continue
        half = float(width.value(0) or 0.25) / 2 if width is not None else 0.125
        a = (float(start.value(0) or 0), float(start.value(1) or 0))
        b = (float(end.value(0) or 0), float(end.value(1) or 0))
        polygon = _swept(a, b, half)
        if polygon:
            obstacles.append(
                Obstacle(
                    name=f"manual:segment#{index}",
                    polygon=polygon,
                    net=net_of(item),
                    layers=frozenset({layer}),
                    kind="track",
                )
            )

    for index, item in enumerate(board.children("via")):
        at = item.child("at")
        size = item.child("size")
        if at is None:
            continue
        radius = float(size.value(0) or 0.6) / 2 if size is not None else 0.3
        centre = (float(at.value(0) or 0), float(at.value(1) or 0))
        spanned = item.child("layers")
        layers = (
            frozenset(a.value for a in spanned.atoms()) if spanned is not None else frozenset()
        )
        obstacles.append(
            Obstacle(
                name=f"manual:via#{index}",
                polygon=tuple(
                    (centre[0] + x, centre[1] + y) for x, y in _circle(radius)
                ),
                net=net_of(item),
                # A barrel is a hole through the board, so it blocks every layer --
                # which is what an empty layer set means to `blocks`.
                layers=frozenset() if len(layers) != 2 else layers,
                kind="via",
            )
        )
    return obstacles


def _swept(a: Point, b: Point, half: float) -> Polygon:
    """A track segment's copper: its centre-line swept by half its width."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return _circle(half) and tuple((a[0] + x, a[1] + y) for x, y in _circle(half))
    nx, ny = -dy / length * half, dx / length * half
    ex, ey = dx / length * half, dy / length * half
    return convex_hull(
        (
            (a[0] + nx - ex, a[1] + ny - ey),
            (a[0] - nx - ex, a[1] - ny - ey),
            (b[0] + nx + ex, b[1] + ny + ey),
            (b[0] - nx + ex, b[1] - ny + ey),
        )
    )


def _courtyard(footprint: SNode, fx: float, fy: float, rotation: float) -> Polygon:
    """A component's body outline, for routes told to pass a whole part."""
    points: list[Point] = []
    for item in footprint.children():
        layer = item.get("layer") or ""
        if not layer.endswith(".CrtYd"):
            continue
        for token in ("start", "end", "center"):
            node = item.child(token)
            if node is not None:
                points.append((float(node.value(0) or 0), float(node.value(1) or 0)))
    if len(points) < 3:
        return ()
    placed = tuple(
        (x + fx, y + fy) for x, y in (_rotate(point, rotation) for point in points)
    )
    return convex_hull(placed)


def _centroid(polygon: Polygon) -> Point:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _reference(footprint: SNode) -> str | None:
    for prop in footprint.children("property"):
        if prop.value(0) == "Reference":
            return prop.value(1)
    return None

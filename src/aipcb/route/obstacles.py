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

from aipcb.kicad.sexpr import SNode

__all__ = [
    "Clearances",
    "Obstacle",
    "Polygon",
    "RoutingEnvironment",
    "board_outline",
    "extract_obstacles",
    "inflate",
]

Point = tuple[float, float]
Polygon = tuple[Point, ...]
#: Looks up a net's clearance, so the larger of two nets' rules can be honoured.
Clearances = Callable[[str | None], float]

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

    def blocks(self, net: str, layer: str) -> bool:
        """Whether this obstacle is in the way of a route on ``net`` and ``layer``.

        A component body does not block copper. A courtyard states how much room a
        part needs on the board, not that its footprint is a copper keep-out --
        running a track under an SMD chip is normal and legal. Bodies are extracted
        anyway so that a sketch can name one as something to pass, which is often
        how a person would describe the route.
        """
        if self.kind == "body":
            return False
        if self.net is not None and self.net == net:
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
        net: str,
        layer: str,
        *,
        clearance: float = 0.2,
        track_width: float = 0.25,
        clearance_of: Clearances | None = None,
    ) -> list[Obstacle]:
        """Obstacles in the way of a route, inflated by the room it must leave.

        KiCad enforces the *larger* of the two nets' clearances between any two
        pieces of copper, so a route through a 0.2 mm-clearance net still has to
        keep 0.25 mm away from a 0.25 mm-clearance power pad. Using only the routing
        net's own figure produced violations short by exactly the difference.
        """
        out: list[Obstacle] = []
        for _, obstacle in sorted(self.obstacles.items()):
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
    if not degrees:
        return point
    theta = math.radians(degrees)
    cos, sin = math.cos(theta), math.sin(theta)
    return (point[0] * cos - point[1] * sin, point[0] * sin + point[1] * cos)


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


def inflate(polygon: Polygon, margin: float) -> Polygon:
    """Grow a convex polygon outward by ``margin`` in every direction.

    Implemented as the convex hull of small circles placed at each vertex -- the
    Minkowski sum of the polygon with a disc, which is exactly the set of points
    within ``margin`` of it. Approximating the disc by a circumscribed polygon keeps
    the result conservative: never smaller than the true offset, so a path that
    clears the inflated hull always clears the real one.
    """
    if margin <= 0 or not polygon:
        return polygon
    disc = _circle(margin)
    return convex_hull(
        tuple((px + dx, py + dy) for px, py in polygon for dx, dy in disc)
    )


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
    """Recover the board edge from its ``Edge.Cuts`` graphics.

    Only straight segments are followed. A board whose edge is drawn with arcs
    still routes -- the outline degrades to the convex hull of the segment
    endpoints, which is conservative for keeping copper inside.
    """
    points: list[Point] = []
    for item in board.children():
        if item.get("layer") != "Edge.Cuts":
            continue
        for token in ("start", "end", "center", "mid"):
            node = item.child(token)
            if node is not None:
                points.append((float(node.value(0) or 0), float(node.value(1) or 0)))
    if len(points) < 3:
        return ()
    return convex_hull(tuple(points))


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

    spun = tuple(_rotate(point, pad_rotation) for point in local)
    placed = tuple(_rotate((x + px, y + py), rotation) for x, y in spun)
    return placed


def extract_obstacles(board: SNode) -> RoutingEnvironment:
    """Build the routing environment from a board, in physical dimensions."""
    environment = RoutingEnvironment(outline=board_outline(board))

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

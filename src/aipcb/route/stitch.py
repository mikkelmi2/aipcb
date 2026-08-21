"""Stitching vias: the barrels that make two pours one plane.

A ground pour on the front and a ground pour on the back are two sheets of copper
until something joins them. Stitching is what joins them, and it is a *pattern* --
a lattice, a row along the edge, a ring around a noisy part -- not a routing
problem. So it is built the way M9e builds fanout escapes (ADR 0008): a
deterministic generator that runs before the fill, whose output is ordinary vias.

Three properties make that work:

**Candidates are a function of the source, not of the copper.** The lattice, the
edge row and the ring are computed from the zones and the outline alone. Which
candidates *survive* depends on the tracks and pads that are in the way, but the
candidate list -- and therefore every UUID -- does not. That is what lets a second
run recognise and replace its own vias rather than pile more on top of them.

**A via must land in copper.** Every candidate has to sit inside the pours of its
net on *both* layers it joins, with room for its own annulus. A stitching via that
misses the pour is not a harmless no-op: it is an isolated piece of copper, which
KiCad reports as an unconnected item, so the rule that keeps boards DRC-clean is
the same rule that makes the stitching mean anything.

**Skips are silent, counts are not.** A position with a track through it is
dropped without ceremony -- that is what a pattern generator is for -- but how many
were dropped goes in the report, because a grid that placed three of forty vias is
telling you something about the board.

Obstacle keys here are per pad *instance* (``J1.6#7``), inherited from
:mod:`aipcb.route.obstacles`, which is the only reason this works on a receptacle
whose twelve shield tabs are all pad 6.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from aipcb.compile.frame import BoardFrame, frame_for
from aipcb.diagnostics import Report
from aipcb.ids import element_uuid, net_codes
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.model.board import tessellate
from aipcb.model.layout import copper_layer_names
from aipcb.model.pours import Stitching
from aipcb.netlist import Netlist
from aipcb.route.geometry import edge_clearance_for, rules_for
from aipcb.route.obstacles import (
    Obstacle,
    Polygon,
    RoutingEnvironment,
    extract_obstacles,
    preserved_copper,
)
from aipcb.source import Loc

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from shapely.geometry.base import BaseGeometry

__all__ = [
    "MAX_STITCH_VIAS",
    "StitchResult",
    "StitchVia",
    "stitch_board",
    "stitch_uuid",
    "stitch_uuids",
]

Point = tuple[float, float]

#: How many candidate positions one pattern may produce. A cap rather than a
#: policy: it bounds both the cost of a mistyped ``pitch: 0.01`` and the range of
#: UUIDs :mod:`aipcb.checks.mapping` has to index to map a violation back to source.
MAX_STITCH_VIAS = 4096

#: Hole-to-hole spacing between two stitching vias, as a multiple of the larger
#: diameter. One diameter keeps the annuli apart, which is the condition KiCad's
#: hole-clearance rule is really about.
_HOLE_SPACING = 1.0

#: Below this the ring pattern has no room for even three vias.
_MIN_RING_RADIUS = 0.5

#: KiCad's default hole-to-hole clearance, in millimetres. Copper clearance does
#: not cover this: two vias of the *same* net may touch electrically, and the
#: router's obstacle model rightly lets them, but a fabricator still has to drill
#: two holes -- so a stitching via keeps this distance from every hole on the
#: board, its own net's included. Measured the hard way: without it, `mcu-4layer`
#: put a stitching barrel 0.21 mm from a routed via and DRC said so.
_HOLE_TO_HOLE = 0.25


def stitch_uuid(pattern: int, ordinal: int) -> str:
    """The stable identity of one stitching via: its pattern, and its place in it."""
    return element_uuid("stitch", pattern, ordinal)


def stitch_uuids(netlist: Netlist) -> set[str]:
    """Every UUID the stitching patterns could produce.

    Used to strip a previous run's vias before this one generates its own -- the
    same trick :func:`aipcb.route.emit.drop_generated` plays with track UUIDs, and
    it works for the same reason: our identifiers are derived, so last run's are
    computable without reading last run's output.
    """
    return {
        stitch_uuid(pattern, ordinal)
        for pattern in range(len(netlist.stitching))
        for ordinal in range(MAX_STITCH_VIAS)
    }


@dataclass(frozen=True, slots=True)
class StitchVia:
    """One placed stitching via."""

    net: str
    point: Point
    drill: float
    diameter: float
    from_layer: str
    to_layer: str
    pattern: int
    ordinal: int

    @property
    def uuid(self) -> str:
        return stitch_uuid(self.pattern, self.ordinal)


@dataclass(slots=True)
class StitchResult:
    """What the generator produced, per pattern and in total."""

    placed: list[StitchVia] = field(default_factory=list)
    skipped: dict[int, int] = field(default_factory=dict)
    candidates: dict[int, int] = field(default_factory=dict)

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped.values())

    def summary(self) -> dict[str, Any]:
        return {
            "placed": len(self.placed),
            "skipped": self.total_skipped,
            "patterns": [
                {
                    "pattern": index,
                    "candidates": self.candidates.get(index, 0),
                    "placed": sum(1 for v in self.placed if v.pattern == index),
                    "skipped": self.skipped.get(index, 0),
                }
                for index in sorted(self.candidates)
            ],
        }


# ---------------------------------------------------------------------------
# the generator
# ---------------------------------------------------------------------------


def stitch_board(board: SNode, netlist: Netlist, report: Report) -> StitchResult:
    """Generate every stitching pattern and add the surviving vias to ``board``.

    Runs after routing and before fill: after, because the tracks are what a
    candidate has to avoid; before, because the fill is what makes the vias part of
    the plane rather than isolated copper.
    """
    result = StitchResult()
    if not netlist.stitching:
        return result

    _drop_previous(board, netlist)

    environment = extract_obstacles(board, edge_clearance=edge_clearance_for(netlist))
    for obstacle in preserved_copper(board):
        environment.obstacles[obstacle.name] = obstacle
    frame = frame_for(netlist)
    codes = net_codes(sorted(netlist.nets))
    layer_names = _copper_layers(netlist)
    accepted: list[StitchVia] = []

    for index, intent in enumerate(netlist.stitching):
        _one_pattern(
            index, intent, board, netlist, environment, frame, layer_names,
            codes, accepted, result, report,
        )

    if result.placed or result.total_skipped:
        report.info(
            "stitching-generated",
            f"{len(result.placed)} stitching via"
            f"{'s' if len(result.placed) != 1 else ''} placed, "
            f"{result.total_skipped} position"
            f"{'s' if result.total_skipped != 1 else ''} skipped",
            hint="a skipped position had a track, a pad, a hole or the board edge "
            "in the way; the pattern is generated, never negotiated",
        )
    return result


def _drop_previous(board: SNode, netlist: Netlist) -> None:
    """Remove the vias a previous run of these same patterns left behind."""
    owned = stitch_uuids(netlist)
    for item in list(board.items):
        if isinstance(item, SNode) and item.name == "via" and item.get("uuid") in owned:
            board.items.remove(item)


def _copper_layers(netlist: Netlist) -> tuple[str, ...]:
    count = netlist.layout.stackup.copper_layers if netlist.layout else 2
    return copper_layer_names(count)


def _one_pattern(
    index: int,
    intent: Stitching,
    board: SNode,
    netlist: Netlist,
    environment: RoutingEnvironment,
    frame: BoardFrame | None,
    layer_names: tuple[str, ...],
    codes: dict[str, int],
    accepted: list[StitchVia],
    result: StitchResult,
    report: Report,
) -> None:
    from shapely.geometry import Point as ShapelyPoint

    path: tuple[str | int, ...] = ("stitching", index)
    loc = netlist.locs.get(path)
    span = _span(intent, layer_names)
    if span is None:
        report.warning(
            "stitching-unknown-layer",
            f"`stitching:` asks to stitch {intent.label} between layers this "
            f"{len(layer_names)}-layer board does not have",
            loc=loc, path=path,
            hint=f"available copper layers: {', '.join(layer_names)}",
        )
        return

    rules = rules_for(netlist, intent.net)
    diameter = intent.via.diameter if intent.via else rules.via_diameter
    drill = intent.via.drill if intent.via else rules.via_drill
    radius = diameter / 2

    area = _stitchable_area(board, netlist, intent, span, radius, environment)
    if area is None or area.is_empty:
        report.warning(
            "stitching-no-plane",
            f"nothing to stitch: {intent.net} has no poured copper shared by "
            f"{span[0]} and {span[1]}",
            loc=loc, path=path,
            hint="a stitching via outside the pour joins nothing and reports as an "
            "unconnected item; declare a `pours:` block covering both layers",
        )
        return

    points = _pattern_points(intent, area, board, netlist, frame, report, path, loc)
    if len(points) > MAX_STITCH_VIAS:
        report.warning(
            "stitching-too-dense",
            f"{intent.label} would place {len(points)} vias; keeping the first "
            f"{MAX_STITCH_VIAS}",
            loc=loc, path=path,
            hint=f"a pitch of {intent.pitch} mm over this area is finer than a "
            "stitching pattern is meant to be; raise it",
        )
        points = points[:MAX_STITCH_VIAS]
    result.candidates[index] = len(points)

    holes = _drilled_holes(board)
    blocking = {
        layer: environment.blocking(
            intent.net,
            layer,
            clearance=rules.clearance,
            track_width=diameter,
            clearance_of=lambda net: rules_for(netlist, net).clearance if net else 0.0,
        )
        for layer in _barrel(span, layer_names)
    }

    skipped = 0
    for ordinal, point in enumerate(points):
        if not area.contains(ShapelyPoint(point)):
            skipped += 1
            continue
        if _too_close(point, diameter, accepted):
            skipped += 1
            continue
        if _fouls_a_hole(point, drill, holes):
            skipped += 1
            continue
        if any(_hits(point, obstacles) for obstacles in blocking.values()):
            skipped += 1
            continue
        via = StitchVia(
            net=intent.net,
            point=point,
            drill=drill,
            diameter=diameter,
            from_layer=span[0],
            to_layer=span[1],
            pattern=index,
            ordinal=ordinal,
        )
        accepted.append(via)
        result.placed.append(via)
        board.add(_via_node(via, codes.get(intent.net, 0)))
    result.skipped[index] = skipped


def _span(intent: Stitching, layer_names: tuple[str, ...]) -> tuple[str, str] | None:
    """The two layers the barrel joins -- the outer pair unless the source says."""
    pair = intent.between or (layer_names[0], layer_names[-1])
    if pair[0] not in layer_names or pair[1] not in layer_names:
        return None
    return (pair[0], pair[1])


def _barrel(span: tuple[str, str], layer_names: tuple[str, ...]) -> tuple[str, ...]:
    """Every layer the drill passes through, which is every layer it can foul."""
    low = layer_names.index(span[0])
    high = layer_names.index(span[1])
    lo, hi = min(low, high), max(low, high)
    return layer_names[lo : hi + 1]


# ---------------------------------------------------------------------------
# where a via is allowed to be
# ---------------------------------------------------------------------------


def _stitchable_area(
    board: SNode,
    netlist: Netlist,
    intent: Stitching,
    span: tuple[str, str],
    radius: float,
    environment: RoutingEnvironment,
) -> BaseGeometry | None:
    """The copper both ends of the barrel can reach, eroded by the via's own annulus.

    Read from the board's zones rather than from the source, for the same reason
    the router reads obstacles from the board: that is where the geometry has
    actually been resolved, and it means a hand-drawn pour stitches like a declared
    one.
    """
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    inside = ShapelyPolygon(environment.outline, environment.cutouts)
    if not inside.is_valid or inside.is_empty:
        return None
    inside = inside.buffer(-environment.edge_clearance)

    area: BaseGeometry = inside
    for layer in dict.fromkeys(span):
        zones = [
            ShapelyPolygon(points)
            for points in _zone_polygons(board, intent.net, layer)
            if len(points) >= 3
        ]
        if not zones:
            return None
        area = area.intersection(unary_union(zones))
    if intent.region is not None and intent.pattern != "ring":
        frame = frame_for(netlist)
        if frame is not None:
            region = ShapelyPolygon(
                [frame.to_kicad(p) for p in tessellate(intent.region.ring())]
            )
            area = area.intersection(region)
    return area.buffer(-radius)


def _zone_polygons(board: SNode, net: str, layer: str) -> list[Polygon]:
    """Every zone boundary on ``layer`` belonging to ``net``, in KiCad coordinates."""
    out: list[Polygon] = []
    for zone in board.children("zone"):
        if zone.get("net_name") != net:
            continue
        layers = {a.value for a in (zone.child("layers") or SNode("x")).atoms()}
        single = zone.get("layer")
        if single is not None:
            layers.add(single)
        if layer not in layers:
            continue
        polygon = zone.child("polygon")
        pts = polygon.child("pts") if polygon is not None else None
        if pts is None:
            continue
        out.append(
            tuple(
                (float(xy.value(0) or 0), float(xy.value(1) or 0))
                for xy in pts.children("xy")
            )
        )
    return out


def _too_close(point: Point, diameter: float, accepted: list[StitchVia]) -> bool:
    """Whether this position would drill into a stitching via already placed."""
    return any(
        math.dist(point, via.point) < max(diameter, via.diameter) * _HOLE_SPACING
        for via in accepted
    )


def _drilled_holes(board: SNode) -> list[tuple[Point, float]]:
    """Every hole already drilled in the board: vias, and through-hole pads."""
    holes: list[tuple[Point, float]] = []
    for via in board.children("via"):
        at = via.child("at")
        drill = via.child("drill")
        if at is None:
            continue
        holes.append(
            (
                (float(at.value(0) or 0), float(at.value(1) or 0)),
                float(drill.value(0) or 0.3) if drill is not None else 0.3,
            )
        )
    for footprint in board.children("footprint"):
        at = footprint.child("at")
        if at is None:
            continue
        fx, fy = float(at.value(0) or 0), float(at.value(1) or 0)
        rotation = float(at.value(2) or 0)
        for pad in footprint.children("pad"):
            drill = pad.child("drill")
            pad_at = pad.child("at")
            if drill is None or pad_at is None:
                continue
            local = (float(pad_at.value(0) or 0), float(pad_at.value(1) or 0))
            holes.append((_place(local, rotation, fx, fy), _drill_size(drill)))
    return holes


def _drill_size(drill: SNode) -> float:
    """A pad's drill, as a diameter. An oval slot is measured across its long axis.

    ``(drill oval 0.9 1.2)`` is a slot, not a hole, and the honest single number
    for keeping other holes away from it is its longest dimension.
    """
    values: list[float] = []
    for atom in drill.atoms():
        try:
            values.append(float(atom.value))
        except ValueError:
            continue
    return max(values) if values else 0.3


def _place(local: Point, rotation: float, fx: float, fy: float) -> Point:
    """A footprint-local point in board coordinates. KiCad's rotation sense."""
    theta = math.radians(rotation)
    cos, sin = math.cos(theta), math.sin(theta)
    return (local[0] * cos + local[1] * sin + fx, -local[0] * sin + local[1] * cos + fy)


def _fouls_a_hole(point: Point, drill: float, holes: list[tuple[Point, float]]) -> bool:
    """Whether drilling here would come too close to a hole the board already has."""
    return any(
        math.dist(point, centre) < (drill + other) / 2 + _HOLE_TO_HOLE
        for centre, other in holes
    )


def _hits(point: Point, obstacles: list[Obstacle]) -> bool:
    """Whether the position falls inside anything the via must keep clear of.

    The obstacles arrive already inflated by the clearance *this* via needs, so a
    point outside all of them is a legal position -- the same trick the rubber-band
    router uses, and the reason no clearance arithmetic happens here.
    """
    return any(_inside(point, obstacle.polygon) for obstacle in obstacles)


def _inside(point: Point, polygon: Polygon) -> bool:
    """Point in convex polygon. Every obstacle polygon is a convex hull."""
    if len(polygon) < 3:
        return False
    x, y = point
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    if not (min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)):
        return False
    sign = 0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if cross > 1e-12:
            if sign < 0:
                return False
            sign = 1
        elif cross < -1e-12:
            if sign > 0:
                return False
            sign = -1
    return True


# ---------------------------------------------------------------------------
# the patterns themselves
# ---------------------------------------------------------------------------


def _pattern_points(
    intent: Stitching,
    area: BaseGeometry,
    board: SNode,
    netlist: Netlist,
    frame: BoardFrame | None,
    report: Report,
    path: tuple[str | int, ...],
    loc: Loc | None,
) -> list[Point]:
    if intent.pattern == "grid":
        return _grid_points(intent, area)
    if intent.pattern == "edge":
        return _edge_points(intent, board, netlist)
    return _ring_points(intent, netlist, board, frame, report, path, loc)


def _grid_points(intent: Stitching, area: BaseGeometry) -> list[Point]:
    """A lattice over the area, anchored to the pitch itself.

    The anchor is a multiple of the pitch in board coordinates rather than the
    area's own corner, so two patterns at the same pitch interlock instead of
    landing a fraction of a millimetre apart.
    """
    min_x, min_y, max_x, max_y = area.bounds
    pitch = intent.pitch
    start_x = math.ceil(min_x / pitch) * pitch
    start_y = math.ceil(min_y / pitch) * pitch
    points: list[Point] = []
    rows = int((max_y - start_y) / pitch) + 1 if max_y >= start_y else 0
    columns = int((max_x - start_x) / pitch) + 1 if max_x >= start_x else 0
    for row in range(max(rows, 0)):
        for column in range(max(columns, 0)):
            points.append(
                (round(start_x + column * pitch, 6), round(start_y + row * pitch, 6))
            )
    return points


def _edge_points(intent: Stitching, board: SNode, netlist: Netlist) -> list[Point]:
    """A row following the board outline, ``inset`` inside it.

    The outline is read back from ``Edge.Cuts`` -- arcs included, tessellated by the
    same reader the router uses -- so a radiused corner gets vias round the curve
    rather than across the chord.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    from aipcb.route.obstacles import board_rings

    outline, cutouts = board_rings(board)
    if len(outline) < 3:
        return []
    shape = ShapelyPolygon(outline, cutouts).buffer(-intent.inset)
    if shape.is_empty:
        return []
    ring = _largest_exterior(shape)
    if ring is None:
        return []
    return _walk(ring, intent.pitch)


def _largest_exterior(shape: BaseGeometry) -> list[Point] | None:
    """The exterior ring of the biggest piece, which is the board's own edge."""
    from shapely.geometry import Polygon as ShapelyPolygon

    pieces = (
        [shape] if isinstance(shape, ShapelyPolygon) else sorted(
            shape.geoms, key=lambda g: (-g.area, g.bounds)
        )
    )
    for piece in pieces:
        if not piece.is_empty:
            return [(float(x), float(y)) for x, y in piece.exterior.coords]
    return None


def _walk(ring: list[Point], pitch: float) -> list[Point]:
    """Points every ``pitch`` millimetres along a closed polyline, from its start."""
    points: list[Point] = []
    travelled = 0.0
    next_stop = 0.0
    for a, b in pairwise(ring):
        span = math.dist(a, b)
        if span < 1e-9:
            continue
        while next_stop <= travelled + span:
            t = (next_stop - travelled) / span
            points.append(
                (round(a[0] + (b[0] - a[0]) * t, 6), round(a[1] + (b[1] - a[1]) * t, 6))
            )
            next_stop += pitch
            if len(points) > MAX_STITCH_VIAS:
                return points
        travelled += span
    return points


def _ring_points(
    intent: Stitching,
    netlist: Netlist,
    board: SNode,
    frame: BoardFrame | None,
    report: Report,
    path: tuple[str | int, ...],
    loc: Loc | None,
) -> list[Point]:
    """A circle of vias around a part or a region -- a fence for a noise source."""
    centre, span = _ring_target(intent, netlist, board, frame)
    if centre is None:
        report.warning(
            "stitching-unknown-target",
            f"`stitching:` rings {intent.around}, which is not a placed component "
            "on this board",
            loc=loc, path=path,
            hint="a ring pattern needs something to go around; check the reference "
            "designator",
        )
        return []
    radius = intent.radius if intent.radius is not None else span + intent.pitch
    if radius < _MIN_RING_RADIUS:
        return []
    count = max(3, round(math.tau * radius / intent.pitch))
    return [
        (
            round(centre[0] + radius * math.cos(math.tau * i / count), 6),
            round(centre[1] + radius * math.sin(math.tau * i / count), 6),
        )
        for i in range(count)
    ]


def _ring_target(
    intent: Stitching, netlist: Netlist, board: SNode, frame: BoardFrame | None
) -> tuple[Point | None, float]:
    """Where the ring is centred, and how far out the thing it circles reaches."""
    from shapely.geometry import Polygon as ShapelyPolygon

    if intent.around is not None:
        refdes = _resolve_part(intent.around, netlist)
        for footprint in board.children("footprint"):
            reference = next(
                (p.value(1) for p in footprint.children("property")
                 if p.value(0) == "Reference"),
                None,
            )
            if reference != refdes:
                continue
            at = footprint.child("at")
            if at is None:  # pragma: no cover - every placed footprint has one
                continue
            centre = (float(at.value(0) or 0), float(at.value(1) or 0))
            return centre, _extent(footprint)
        return None, 0.0

    if intent.region is None or frame is None:  # pragma: no cover - validated earlier
        return None, 0.0
    points = [frame.to_kicad(p) for p in tessellate(intent.region.ring())]
    shape = ShapelyPolygon(points)
    centre = (float(shape.centroid.x), float(shape.centroid.y))
    return centre, max(math.dist(centre, p) for p in points)


def _resolve_part(name: str, netlist: Netlist) -> str:
    for component in netlist.components.values():
        if component.path_text == name or component.refdes == name:
            return component.refdes
    return name


def _extent(footprint: SNode) -> float:
    """How far the part's own copper and courtyard reach from its origin."""
    reach = 0.0
    for pad in footprint.children("pad"):
        at = pad.child("at")
        size = pad.child("size")
        if at is None:
            continue
        offset = math.hypot(float(at.value(0) or 0), float(at.value(1) or 0))
        half = 0.0
        if size is not None:
            half = math.hypot(float(size.value(0) or 0), float(size.value(1) or 0)) / 2
        reach = max(reach, offset + half)
    for item in footprint.children():
        if not (item.get("layer") or "").endswith(".CrtYd"):
            continue
        for token in ("start", "end", "center"):
            node = item.child(token)
            if node is not None:
                reach = max(
                    reach,
                    math.hypot(float(node.value(0) or 0), float(node.value(1) or 0)),
                )
    return reach


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------


def _via_node(via: StitchVia, code: int) -> SNode:
    """One stitching via, written exactly as any other through via is."""
    return SNode("via").add(
        SNode("at").add(num(via.point[0]), num(via.point[1])),
        SNode("size").add(num(via.diameter)),
        SNode("drill").add(num(via.drill)),
        SNode("layers").add(quoted(via.from_layer), quoted(via.to_layer)),
        SNode("net").add(sym(str(code))),
        SNode("uuid").add(quoted(via.uuid)),
    )

"""Plane integrity: what the fill actually produced, measured off the filled board.

A pour is a request. What comes back is copper with holes in it, cut by every track
that crossed the plane and every pad that needed clearance, and the interesting
question -- the one an agent editing the source cannot answer from the YAML -- is
whether the result is still *one* piece of copper. A ground plane sliced in two by
a track is electrically two planes, and the return current of everything above it
has to go the long way round.

So this reads the ``filled_polygon`` nodes back out of the checked board and does
connectivity analysis on them. Facts, not a verdict: a fragmented plane is often
perfectly fine and only the designer knows whether this one is, so the numbers are
reported and only an explicit ``min_contiguous:`` in the source turns fragmentation
into a *warning*. Never an error.

Everything here is a pure function of a parsed board plus the netlist. Nothing runs
KiCad, nothing writes a file, and the fill it analyses is the same fill DRC just
checked -- which is the whole reason the numbers mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aipcb.compile.zones import zone_uuid
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import Netlist
from aipcb.route.obstacles import board_rings

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from shapely.geometry.base import BaseGeometry

__all__ = [
    "Island",
    "PlaneLayer",
    "PlaneReport",
    "analyse_planes",
    "report_planes",
]

Point = tuple[float, float]
Box = tuple[float, float, float, float]

#: Filled areas smaller than this are rounding, not copper.
_AREA_EPSILON = 1e-9
#: How many island bounding boxes a diagnostic will list before it stops.
_MAX_LISTED = 6


@dataclass(frozen=True, slots=True)
class Island:
    """One connected piece of poured copper."""

    area_mm2: float
    bbox: Box
    """``(min_x, min_y, max_x, max_y)`` in KiCad board coordinates."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "area_mm2": round(self.area_mm2, 4),
            "bbox": [round(v, 4) for v in self.bbox],
        }


@dataclass(frozen=True, slots=True)
class PlaneLayer:
    """One pour's copper on one layer."""

    layer: str
    islands: tuple[Island, ...]
    scope_mm2: float
    """The area the pour asked for: its outline, clipped to the board."""

    @property
    def filled_mm2(self) -> float:
        return sum(island.area_mm2 for island in self.islands)

    @property
    def largest_mm2(self) -> float:
        return max((island.area_mm2 for island in self.islands), default=0.0)

    @property
    def contiguous(self) -> float:
        """Largest island as a fraction of the copper that was actually poured.

        This is the fragmentation number, and it is deliberately *not* measured
        against the pour's scope. A plane on a busy board never fills its whole
        scope -- pads, tracks and clearances take a third of it on a dense
        two-layer board -- so a scope fraction says more about component density
        than about whether the plane is in one piece. ``min_contiguous:`` compares
        against this.
        """
        total = self.filled_mm2
        return self.largest_mm2 / total if total > _AREA_EPSILON else 0.0

    @property
    def coverage(self) -> float:
        """Largest island as a fraction of the pour's scope, which M10d also asks for."""
        return self.largest_mm2 / self.scope_mm2 if self.scope_mm2 > _AREA_EPSILON else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "islands": len(self.islands),
            "filled_mm2": round(self.filled_mm2, 4),
            "scope_mm2": round(self.scope_mm2, 4),
            "largest_mm2": round(self.largest_mm2, 4),
            "contiguous": round(self.contiguous, 4),
            "coverage": round(self.coverage, 4),
            "island_bboxes": [i.to_dict() for i in self.islands[:_MAX_LISTED]],
        }


@dataclass(frozen=True, slots=True)
class PlaneReport:
    """Everything measured about one pour after the fill."""

    pour: int
    """Its index in ``pours:`` -- the same index its source path uses."""
    net: str
    label: str
    layers: tuple[PlaneLayer, ...]
    min_contiguous: float | None = None
    islands_removed: int = 0
    """Islands island-removal deleted, when the fill measured the comparison."""
    area_removed_mm2: float = 0.0

    @property
    def islands(self) -> int:
        return sum(len(layer.islands) for layer in self.layers)

    @property
    def contiguous(self) -> float:
        """The worst layer's fragmentation, since a plane is only as good as its worst."""
        return min((layer.contiguous for layer in self.layers), default=0.0)

    @property
    def fragmented(self) -> bool:
        return (
            self.min_contiguous is not None
            and self.contiguous < self.min_contiguous
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pour": self.pour,
            "net": self.net,
            "label": self.label,
            "islands": self.islands,
            "contiguous": round(self.contiguous, 4),
            "min_contiguous": self.min_contiguous,
            "islands_removed": self.islands_removed,
            "area_removed_mm2": round(self.area_removed_mm2, 4),
            "layers": [layer.to_dict() for layer in self.layers],
        }


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def analyse_planes(
    board: SNode,
    netlist: Netlist,
    *,
    removed: dict[str, tuple[int, float]] | None = None,
) -> list[PlaneReport]:
    """Measure every pour's filled copper on a filled board.

    ``removed`` maps a zone UUID to the islands and area island-removal deleted, as
    the fill stage measured it. It is optional: without it the report simply does
    not claim anything about removal, which is better than guessing.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    if not netlist.pours:
        return []
    outline, cutouts = board_rings(board)
    inside: BaseGeometry | None = None
    if len(outline) >= 3:
        candidate = ShapelyPolygon(outline, cutouts)
        inside = candidate if candidate.is_valid else candidate.buffer(0)

    zones = {
        uuid: zone
        for zone in board.children("zone")
        if (uuid := zone.get("uuid")) is not None
    }
    reports: list[PlaneReport] = []
    for index, pour in enumerate(netlist.pours):
        zone = zones.get(zone_uuid(index))
        if zone is None:
            continue
        scope = _scope(zone, inside)
        layers = tuple(
            PlaneLayer(
                layer=layer,
                islands=_islands(polygons),
                scope_mm2=scope,
            )
            for layer, polygons in _filled_by_layer(zone).items()
        )
        was_islands, was_area = (removed or {}).get(zone_uuid(index), (0, 0.0))
        reports.append(
            PlaneReport(
                pour=index,
                net=pour.net,
                label=pour.label,
                layers=layers,
                min_contiguous=pour.min_contiguous,
                islands_removed=was_islands,
                area_removed_mm2=was_area,
            )
        )
    return reports


def _scope(zone: SNode, inside: BaseGeometry | None) -> float:
    """How much board the pour asked for: its own outline, clipped to the board."""
    from shapely.geometry import Polygon as ShapelyPolygon

    polygon = zone.child("polygon")
    pts = polygon.child("pts") if polygon is not None else None
    if pts is None:
        return 0.0
    points = [
        (float(xy.value(0) or 0), float(xy.value(1) or 0)) for xy in pts.children("xy")
    ]
    if len(points) < 3:
        return 0.0
    shape: BaseGeometry = ShapelyPolygon(points)
    if not shape.is_valid:
        shape = shape.buffer(0)
    if inside is not None:
        shape = shape.intersection(inside)
    return float(shape.area)


def _filled_by_layer(zone: SNode) -> dict[str, list[list[Point]]]:
    """The zone's filled polygons, grouped by the layer KiCad wrote them on.

    A multi-layer zone carries one ``filled_polygon`` per island *per layer*, each
    tagged with its own layer, so grouping is the difference between "this plane
    is in three pieces" and "this plane is in one piece on each of three layers".
    """
    out: dict[str, list[list[Point]]] = {}
    declared = [a.value for a in (zone.child("layers") or SNode("x")).atoms()]
    single = zone.get("layer")
    for name in ([single] if single else []) + declared:
        if name:
            out.setdefault(name, [])
    for filled in zone.children("filled_polygon"):
        layer = filled.get("layer") or single or ""
        pts = filled.child("pts")
        if pts is None:
            continue
        out.setdefault(layer, []).append(
            [
                (float(xy.value(0) or 0), float(xy.value(1) or 0))
                for xy in pts.children("xy")
            ]
        )
    return dict(sorted(out.items()))


def _islands(polygons: list[list[Point]]) -> tuple[Island, ...]:
    """Connected pieces of copper, largest first.

    The polygons are unioned rather than counted, because KiCad writes a fractured
    polygon set: a shape with a hole comes back as several outlines joined by a
    hairline slit, and counting nodes would report one plane as four.
    """
    from shapely.geometry import MultiPolygon
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    shapes = []
    for points in polygons:
        if len(points) < 3:
            continue
        shape = ShapelyPolygon(points)
        if not shape.is_valid:
            shape = shape.buffer(0)
        if not shape.is_empty:
            shapes.append(shape)
    if not shapes:
        return ()
    merged = unary_union(shapes)
    pieces = list(merged.geoms) if isinstance(merged, MultiPolygon) else [merged]
    islands = [
        Island(
            area_mm2=float(piece.area),
            bbox=tuple(round(float(v), 4) for v in piece.bounds),  # type: ignore[arg-type]
        )
        for piece in pieces
        if piece.area > _AREA_EPSILON
    ]
    return tuple(sorted(islands, key=lambda i: (-i.area_mm2, i.bbox)))


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def report_planes(
    reports: list[PlaneReport], netlist: Netlist, report: Report
) -> None:
    """Turn the measurements into diagnostics pointing at the pours that made them."""
    for plane in reports:
        path: tuple[str | int, ...] = ("pours", plane.pour)
        loc = netlist.locs.get(path)
        pieces = ", ".join(
            f"{layer.layer}: {len(layer.islands)} island"
            f"{'s' if len(layer.islands) != 1 else ''}, "
            f"largest {layer.contiguous:.1%} of the copper and "
            f"{layer.coverage:.1%} of the scope"
            for layer in plane.layers
        )
        report.info(
            "plane-integrity",
            f"{plane.net} pour -- {pieces}",
            loc=loc,
            path=path,
            hint="island bounding boxes are in the JSON report under "
            "`summary.planes`",
            islands=plane.islands,
            contiguous=round(plane.contiguous, 4),
        )
        if plane.islands_removed:
            report.info(
                "plane-islands-removed",
                f"island removal deleted {plane.islands_removed} piece"
                f"{'s' if plane.islands_removed != 1 else ''} of {plane.net} copper "
                f"({plane.area_removed_mm2:.2f} mm2) that reached no pad",
                loc=loc,
                path=path,
                hint="the plane is thinner than the pour's outline suggests; set "
                "`remove_islands: never` to keep them, or stitch them to the rest",
            )
        if plane.fragmented:
            worst = min(plane.layers, key=lambda layer: layer.contiguous)
            report.warning(
                "plane-fragmented",
                f"the {plane.net} pour on {worst.layer} is in "
                f"{len(worst.islands)} pieces; its largest holds "
                f"{worst.contiguous:.1%} of the copper, below the "
                f"{plane.min_contiguous:.1%} this pour asks for",
                loc=loc,
                path=path,
                hint="a track crossing the plane is the usual cause; move it, give "
                "it a layer of its own, or stitch the pieces together",
                islands=len(worst.islands),
                contiguous=round(worst.contiguous, 4),
            )

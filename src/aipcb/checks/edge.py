"""Card-edge connector integration checks (M11b).

Everything here is validation and glue. The fingers are the footprint's; the notch
is the footprint's; what this module does is make sure the board the source
describes is the board that footprint needs.

Four checks, in the order a reader wants them:

1. **the footprint is edge-fixed** -- a card edge is mechanical law, not a
   preference, so it wants a ``fixed:`` placement rather than a region;
2. **the outline agrees with the footprint**, segment by segment. The footprint
   draws its own ``Edge.Cuts``: the tongue, the chamfered tip, the keying notch.
   aipcb does not emit that geometry (see :mod:`aipcb.compile.edge`), so the
   ``board:`` block has to reproduce it, and when it does not the error message
   hands back the vertices that are missing, in the source's own frame and in the
   source's own syntax;
3. **the board is the thickness the slot expects** -- a warning, because a
   fabricator will build 1.6 mm and it will very probably work;
4. **the fab note**: the leading-edge bevel is a process step, not geometry aipcb
   can produce, and a card that reaches a fabricator without it on the drawing
   comes back unusable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aipcb.compile.edge import EDGE_ROLE, EdgeConnector, edge_connectors
from aipcb.compile.frame import BoardFrame, frame_for
from aipcb.compile.place import component_extents, plan_placement
from aipcb.diagnostics import Report
from aipcb.netlist import Netlist

__all__ = ["CARD_EDGE_SLOTS", "run_edge_checks"]

Point = tuple[float, float]

#: How far a footprint's own edge geometry may sit from the declared board edge
#: before it counts as missing. Ten microns: tighter than any fabricator's
#: tolerance, and looser than the rounding a hand-typed vertex carries.
EDGE_TOLERANCE_MM = 0.01

#: Card thickness the slot expects, per edge footprint, as ``(nominal, tolerance)``
#: in millimetres. From the connector specifications rather than from KiCad, which
#: says nothing about how thick the card is: PCI Express CEM gives the card edge as
#: 1.57 mm +/- 0.10 mm. A footprint not listed here gets no thickness check and
#: says so, because inventing a tolerance is worse than not having one.
CARD_EDGE_SLOTS: dict[str, tuple[float, float]] = {
    "Connector_PCBEdge:BUS_PCIexpress_x1": (1.57, 0.10),
    "Connector_PCBEdge:BUS_PCIexpress_x4": (1.57, 0.10),
    "Connector_PCBEdge:BUS_PCIexpress_x8": (1.57, 0.10),
    "Connector_PCBEdge:BUS_PCIexpress_x16": (1.57, 0.10),
}


@dataclass(frozen=True, slots=True)
class _Run:
    """A stretch of the footprint's edge geometry the board outline does not have."""

    points: tuple[Point, ...]

    @property
    def box(self) -> tuple[float, float, float, float]:
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        return (min(xs), min(ys), max(xs), max(ys))


def run_edge_checks(netlist: Netlist, report: Report) -> None:
    """Every edge-connector check. A no-op on a design with no edge connector."""
    if not netlist.components_with_role(EDGE_ROLE):
        return
    frame = frame_for(netlist)
    if frame is None:
        # `_placement_has_a_frame` already errors on this; a second complaint about
        # the same absence is noise.
        return
    extents, missing = component_extents(netlist)
    if missing:
        return
    placement = plan_placement(netlist, report=None, extents=extents, frame=frame)

    for connector in edge_connectors(netlist, placement, frame):
        _check_fixed(netlist, connector, report)
        _check_outline(netlist, connector, frame, report)
        _check_thickness(netlist, connector, report)
        _fab_note(netlist, connector, frame, report)


# ---------------------------------------------------------------------------
# 1. edge-fixed
# ---------------------------------------------------------------------------


def _check_fixed(netlist: Netlist, connector: EdgeConnector, report: Report) -> None:
    entry = netlist.placement.get(connector.refdes)
    if entry is not None and entry.level == "fixed":
        return
    report.error(
        "edge-connector-not-fixed",
        f"{connector.refdes} has `role: edge_connector` but is not placed with a "
        "`fixed:` position",
        loc=netlist.mech_loc("placement", connector.refdes),
        path=netlist.mech_path("placement", connector.refdes),
        hint="a card edge is mechanical law: its fingers have to coincide with the "
        "board outline, so the placer must not be free to move it. Give it "
        "`fixed: { x: ..., y: ..., rot: ... }`",
        component=connector.refdes,
    )


# ---------------------------------------------------------------------------
# 2. the outline agrees with the footprint
# ---------------------------------------------------------------------------


def _check_outline(
    netlist: Netlist, connector: EdgeConnector, frame: BoardFrame, report: Report
) -> None:
    """Every ``Edge.Cuts`` primitive the footprint draws must be on the board edge."""
    edges = _board_edges(frame)
    if not edges:
        return
    unmatched: list[_Run] = []
    matched = 0
    total = 0
    for path in connector.paths:
        run: list[Point] = []
        for point in path:
            total += 1
            if _distance_to_edges(point, edges) <= EDGE_TOLERANCE_MM:
                matched += 1
                if run:
                    unmatched.append(_Run(tuple(run)))
                    run = []
            else:
                run.append(point)
        if run:
            unmatched.append(_Run(tuple(run)))

    if not unmatched:
        report.info(
            "edge-connector-outline-matches",
            f"{connector.refdes}: the board outline reproduces all "
            f"{len(connector.paths)} of the footprint's edge primitives, to within "
            f"{EDGE_TOLERANCE_MM} mm",
            loc=netlist.mech_loc("placement", connector.refdes),
            path=("board", "outline"),
            component=connector.refdes,
        )
        return

    stray = sum(len(run.points) for run in unmatched)
    box = _combined_box(unmatched)
    off_edge = matched == 0
    report.error(
        "edge-connector-off-edge" if off_edge else "edge-connector-notch-missing",
        f"{connector.refdes}'s footprint draws board edge the outline does not "
        f"have: {stray} of {total} sampled points are more than "
        f"{EDGE_TOLERANCE_MM} mm from any declared edge"
        + (
            ", and none of them is on it -- the connector is not on the board edge "
            "at all"
            if off_edge
            else f", in {len(unmatched)} run(s) around "
            f"[{box[0]:.2f}, {box[1]:.2f}]-[{box[2]:.2f}, {box[3]:.2f}]"
        ),
        loc=netlist.locs.get(("board", "outline")) or netlist.locs.get(("board",)),
        path=("board", "outline"),
        hint=_suggestion(unmatched, frame, connector),
        component=connector.refdes,
        unmatched_points=stray,
        sampled_points=total,
    )


def _board_edges(frame: BoardFrame) -> list[tuple[Point, Point]]:
    """Every declared edge as a segment, in KiCad coordinates."""
    segments: list[tuple[Point, Point]] = []
    for ring in (frame.polygon(), *frame.cutout_polygons()):
        points = list(ring)
        if len(points) < 2:
            continue
        for a, b in zip(points, [*points[1:], points[0]], strict=True):
            segments.append((a, b))
    return segments


def _distance_to_edges(point: Point, edges: list[tuple[Point, Point]]) -> float:
    best = float("inf")
    for a, b in edges:
        best = min(best, _point_to_segment(point, a, b))
        if best <= EDGE_TOLERANCE_MM:
            break
    return best


def _point_to_segment(point: Point, a: Point, b: Point) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    span = dx * dx + dy * dy
    if span <= 0:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / span))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def _reach(
    origin: Point, direction: Point, edges: list[tuple[Point, Point]]
) -> float | None:
    """How far it is from ``origin`` to the board edge, straight out along a finger.

    A ray rather than a nearest-point query, because the pad next to a keying notch
    is nearer the notch than it is the card's leading edge, and it is the leading
    edge the bevel goes on.
    """
    best: float | None = None
    for a, b in edges:
        hit = _ray_hit(origin, direction, a, b)
        if hit is not None and (best is None or hit < best):
            best = hit
    return best


def _ray_hit(
    origin: Point, direction: Point, a: Point, b: Point
) -> float | None:
    """Distance from ``origin`` along ``direction`` to the segment ``a``-``b``."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    denominator = direction[0] * ey - direction[1] * ex
    if abs(denominator) < 1e-12:
        return None
    ox, oy = a[0] - origin[0], a[1] - origin[1]
    t = (ox * ey - oy * ex) / denominator
    u = (ox * direction[1] - oy * direction[0]) / -denominator
    if t < 0 or not (0.0 <= u <= 1.0):
        return None
    return t


def _combined_box(runs: list[_Run]) -> tuple[float, float, float, float]:
    boxes = [run.box for run in runs]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _suggestion(
    runs: list[_Run], frame: BoardFrame, connector: EdgeConnector
) -> str:
    """A copy-pasteable ``board:`` block for the geometry that is missing.

    The vertices are the footprint's own, converted back into the source's Y-up
    frame, so the fix is a paste rather than a calculation. Which block they belong
    in depends on where they are: geometry that touches the boundary is part of the
    outline -- a keying notch opens onto the card's leading edge and cannot be a
    hole -- and geometry that does not is a cutout.
    """
    points = [frame.to_source(p) for run in runs for p in run.points]
    ordered = _thin(points)
    listing = "\n".join(f"      - [{x:.2f}, {y:.2f}]" for x, y in ordered)
    return (
        f"{connector.lib_id} draws this edge itself and aipcb does not emit it, so "
        "the `board:` block has to. The footprint's own vertices, in the source's "
        "frame:\n"
        "  board:\n"
        "    outline:\n"
        "      polygon:\n"
        f"{listing}\n"
        "  Splice them into the outline where the card edge runs, or -- if they "
        "form an island away from the boundary -- add them as a `cutouts:` polygon "
        "with a `reason:`."
    )


def _thin(points: list[Point], limit: int = 24) -> list[Point]:
    """At most ``limit`` vertices, evenly spaced, so a hint stays readable."""
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(i * step)] for i in range(limit)]


# ---------------------------------------------------------------------------
# 3. board thickness against the slot
# ---------------------------------------------------------------------------


def _check_thickness(
    netlist: Netlist, connector: EdgeConnector, report: Report
) -> None:
    spec = CARD_EDGE_SLOTS.get(connector.lib_id)
    stackup = netlist.layout.stackup if netlist.layout is not None else None
    thickness = stackup.thickness_mm if stackup is not None else 1.6
    if spec is None:
        report.info(
            "edge-connector-thickness-unknown",
            f"{connector.refdes}: no card thickness is recorded for "
            f"{connector.lib_id}, so the {thickness} mm board is not checked "
            "against the slot",
            loc=netlist.mech_loc("placement", connector.refdes),
            component=connector.refdes,
        )
        return
    nominal, tolerance = spec
    if abs(thickness - nominal) <= tolerance:
        return
    report.warning(
        "edge-connector-thickness",
        f"{connector.refdes}: the slot {connector.lib_id} mates with wants a "
        f"{nominal} +/- {tolerance} mm card, and this board is "
        f"{thickness} mm",
        loc=netlist.locs.get(("layout",)),
        path=("layout", "stackup", "thickness_mm"),
        hint="1.6 mm is the usual stock and most slots take it; this is a warning "
        "rather than an error because the fabricator, not the file, decides the "
        "finished thickness",
        component=connector.refdes,
    )


# ---------------------------------------------------------------------------
# 4. the fab note
# ---------------------------------------------------------------------------


def _fab_note(
    netlist: Netlist, connector: EdgeConnector, frame: BoardFrame, report: Report
) -> None:
    """What the fabricator has to be told, measured off the footprint."""
    if not connector.fingers:
        return
    edges = _board_edges(frame)
    reaches = [
        distance - math.dist(finger.centre, finger.inner)
        for finger in connector.fingers
        if (distance := _reach(finger.centre, finger.outward, edges)) is not None
    ]
    inset = min(reaches) if reaches else 0.0
    layers = sorted({finger.layer for finger in connector.fingers})
    report.info(
        "edge-connector-fab-note",
        f"{connector.refdes}: {len(connector.fingers)} fingers on "
        f"{', '.join(layers)}, set back {max(inset, 0.0):.2f} mm from the card's "
        "leading edge. The set-back is where the bevel goes",
        loc=netlist.mech_loc("placement", connector.refdes),
        path=netlist.mech_path("placement", connector.refdes),
        hint="tell the fabricator: gold plating on the finger area, and a 20-degree "
        "bevel on the leading edge. Neither is geometry aipcb can put in a Gerber, "
        "and a card that arrives without them does not go into a slot",
        component=connector.refdes,
    )

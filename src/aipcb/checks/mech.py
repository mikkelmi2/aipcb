"""Mechanical conflict checks -- the ones that are cheap now and expensive in KiCad.

Two fixed footprints whose courtyards overlap, a connector pinned outside the board
edge, a mounting hole sitting where the flex-tail window is: every one of these is a
statement the source makes about physical space that cannot be true. None of them
needs a build to detect, and all of them cost an hour to find by opening the board
and wondering why it looks wrong.

So they run in ``aipcb validate``, against geometry alone.

One check is deliberately weaker than the rest. Deciding whether a set of distance
constraints is satisfiable is not a cheap question, and a validator that cries
"impossible" about something achievable is worse than no validator at all. So the
relative-intent check reasons with intervals -- the closest two allowed sets can
possibly be -- and speaks only when that lower bound already exceeds what the
constraint asks for. It reports a warning, never an error, because being sure is
not on offer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aipcb.compile.frame import BoardFrame, frame_for
from aipcb.compile.place import component_extents, courtyard_box, plan_placement
from aipcb.diagnostics import Report
from aipcb.kicad.footprints import Extent
from aipcb.model.board import ArcTo, Vertex, arc_radii, vertex_point
from aipcb.model.mech import MechPlacement
from aipcb.netlist import Netlist

__all__ = ["run_mechanical_checks"]

Box = tuple[float, float, float, float]

#: Two lengths that ought to be equal are, if they agree to this. A hundredth of a
#: fabricator's tolerance, and looser than the rounding a hand-typed centre carries.
_TOLERANCE = 1e-4

#: Areas smaller than this are floating-point noise, not an overlap.
_AREA_EPSILON = 1e-9


def run_mechanical_checks(netlist: Netlist, report: Report) -> None:
    """Every mechanical check, in the order a reader would want them."""
    _check_unknown_refs(netlist, report)
    _check_reasons(netlist, report)
    frame = frame_for(netlist)
    if frame is None:
        return
    if not _check_outline(netlist, frame, report):
        # Nothing downstream can be trusted against a boundary that is not a shape.
        return
    _check_cutouts(netlist, frame, report)

    extents = _extents(netlist)
    if extents is None:
        return
    _check_placed_parts(netlist, frame, extents, report)
    _check_allowed_sets(netlist, frame, extents, report)
    _check_relative_intents(netlist, frame, extents, report)


# ---------------------------------------------------------------------------
# the source's own words
# ---------------------------------------------------------------------------


def _check_unknown_refs(netlist: Netlist, report: Report) -> None:
    """A mechanical block that names nothing is a typo, not a constraint."""
    known = ", ".join(sorted(netlist.components)[:10])
    for block, name in netlist.unknown_mech_refs:
        report.error(
            "unknown-mechanical-member",
            f"`{block}:` names {name!r}, which is not a component in this design",
            loc=netlist.locs.get((block, name)),
            path=(block, name),
            hint=f"components available: {known}",
        )


def _check_reasons(netlist: Netlist, report: Report) -> None:
    """A position that cannot be moved has to say why, or nobody dares move it.

    A warning rather than an error, and skipped when the entry carries a ``role``:
    ``role: mounting_hole`` explains itself, and demanding prose for it would train
    people to write prose nobody reads.
    """
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        if entry.fixed is None or entry.reason or entry.role:
            continue
        report.warning(
            "fixed-placement-without-reason",
            f"{refdes} is fixed at ({entry.fixed.x}, {entry.fixed.y}) with no reason "
            "given",
            loc=netlist.mech_loc("placement", refdes),
            path=netlist.mech_path("placement", refdes),
            hint="say what the position is for -- an enclosure opening, a light "
            "pipe, a bolt circle -- and point at the mechanical file if there is "
            "one; a `role:` will do instead",
        )
    for index, cutout in enumerate(netlist.board.cutouts if netlist.board else ()):
        if cutout.reason:
            continue
        report.warning(
            "cutout-without-reason",
            f"the {cutout.label} cutout says nothing about why it is there",
            loc=netlist.locs.get(("board", "cutouts", index)),
            path=("board", "cutouts", index),
            hint="a hole is mechanical law, not reclaimable routing area, and the "
            "next reader has no way to tell the difference without a reason",
        )


# ---------------------------------------------------------------------------
# the boundary itself
# ---------------------------------------------------------------------------


def _check_outline(netlist: Netlist, frame: BoardFrame, report: Report) -> bool:
    """Whether the outline is a shape at all. Returns False if it is not."""
    from shapely.geometry import Polygon as ShapelyPolygon

    _check_arcs(netlist, report)
    ring = frame.polygon()
    if len(ring) < 3:
        report.error(
            "board-outline-degenerate",
            "the board outline has fewer than three corners, so it encloses nothing",
            loc=netlist.locs.get(("board", "outline")),
            path=("board", "outline"),
            hint="give the outline a `rect: [width, height]` or at least three "
            "polygon vertices",
        )
        return False

    shape = ShapelyPolygon(ring)
    if shape.is_valid and shape.exterior.is_simple:
        return True
    report.error(
        "board-outline-self-intersecting",
        "the board outline crosses itself, so there is no inside to it",
        loc=netlist.locs.get(("board", "outline")),
        path=("board", "outline"),
        hint="the vertices are joined in the order they are written; a figure-of-"
        "eight usually means two of them are the wrong way round",
    )
    return False


def _check_arcs(netlist: Netlist, report: Report) -> None:
    """An arc whose two ends are different distances from its centre is not an arc."""
    board = netlist.board
    if board is None:
        return
    rings: list[tuple[tuple[str | int, ...], tuple[Vertex, ...]]] = [
        (("board", "outline"), board.outline.polygon)
    ]
    for index, cutout in enumerate(board.cutouts):
        rings.append((("board", "cutouts", index), cutout.polygon))

    for path, vertices in rings:
        if not vertices:
            continue
        points = [vertex_point(v) for v in vertices]
        for index, vertex in enumerate(vertices):
            if not isinstance(vertex, ArcTo):
                continue
            start, end = arc_radii(vertex, points[index - 1])
            if abs(start - end) <= _TOLERANCE:
                continue
            report.error(
                "board-arc-inconsistent",
                f"the arc to {list(vertex.arc_to)} is {start:.4f} mm from its centre "
                f"at one end and {end:.4f} mm at the other, so no circle passes "
                "through both",
                loc=netlist.locs.get(path),
                path=(*path, index),
                hint="move the centre, or move one of the two ends; a fillet's "
                "centre sits one radius in from each of the edges it joins",
            )


def _check_cutouts(netlist: Netlist, frame: BoardFrame, report: Report) -> None:
    """A hole has to be in the board, and there has to be board left between holes."""
    from shapely.geometry import Polygon as ShapelyPolygon

    board = ShapelyPolygon(frame.polygon())
    shapes = [ShapelyPolygon(ring) for ring in frame.cutout_polygons()]
    cutouts = netlist.board.cutouts if netlist.board else ()

    for index, shape in enumerate(shapes):
        label = cutouts[index].label if index < len(cutouts) else f"cutout {index}"
        loc = netlist.locs.get(("board", "cutouts", index))
        if not board.covers(shape):
            outside = round(shape.difference(board).area, 3)
            report.error(
                "cutout-outside-outline",
                f"the {label} cutout reaches {outside} mm² outside the board outline",
                loc=loc,
                path=("board", "cutouts", index),
                hint="a cutout is a hole through the board; a shape that pokes past "
                "the edge is a change to the edge, and belongs in `outline:`",
            )
        for other in range(index + 1, len(shapes)):
            overlap = shape.intersection(shapes[other]).area
            if overlap <= _AREA_EPSILON:
                continue
            other_label = (
                cutouts[other].label if other < len(cutouts) else f"cutout {other}"
            )
            report.error(
                "cutouts-overlap",
                f"the {label} and {other_label} cutouts overlap by "
                f"{round(overlap, 3)} mm²",
                loc=loc,
                path=("board", "cutouts", index),
                hint="two holes that share area are one hole; merge them into a "
                "single `polygon:` cutout so the edge that gets milled is the edge "
                "the source describes",
            )


# ---------------------------------------------------------------------------
# parts against the boundary and against each other
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Placed:
    refdes: str
    box: Box
    fixed: bool


def _extents(netlist: Netlist) -> dict[str, Extent] | None:
    """Every component's courtyard, or ``None`` if the libraries are not installed.

    Without them there is nothing to say about courtyards, and the missing-footprint
    diagnostic that `check_kicad_bindings` already produces is the useful one.
    """
    extents, missing = component_extents(netlist)
    return None if missing else extents


def _check_placed_parts(
    netlist: Netlist, frame: BoardFrame, extents: dict[str, Extent], report: Report
) -> None:
    """Courtyards against the board, against the cutouts, and against each other."""
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.geometry import box as shapely_box

    placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
    board = ShapelyPolygon(frame.polygon())
    holes = [ShapelyPolygon(ring) for ring in frame.cutout_polygons()]
    fixed = set(netlist.fixed_refs())

    placed: list[_Placed] = []
    for refdes in sorted(placement.positions):
        position = placement.positions[refdes]
        extent = extents.get(refdes)
        if extent is None:
            continue
        placed.append(
            _Placed(
                refdes,
                courtyard_box(extent, position.x, position.y, position.rotation),
                refdes in fixed,
            )
        )

    for entry in placed:
        shape = shapely_box(*entry.box)
        if entry.fixed and not board.covers(shape):
            outside = round(shape.difference(board).area, 3)
            report.error(
                "fixed-part-outside-outline",
                f"{entry.refdes} is fixed where {outside} mm² of its courtyard falls "
                "outside the board outline",
                loc=netlist.mech_loc("placement", entry.refdes),
                path=netlist.mech_path("placement", entry.refdes, "fixed"),
                hint="the outline is the real polygon, not its bounding box; a part "
                "in the missing corner of an L-shaped board is off the board",
            )
        for index, hole in enumerate(holes):
            overlap = shape.intersection(hole).area
            if overlap <= _AREA_EPSILON:
                continue
            where = (
                netlist.mech_path("placement", entry.refdes)
                if entry.fixed
                else ("board", "cutouts", index)
            )
            report.error(
                "part-over-cutout",
                f"{entry.refdes}'s courtyard covers {round(overlap, 3)} mm² of a "
                "cutout, where there is no board to solder it to",
                loc=netlist.locs.get(("board", "cutouts", index)),
                path=where,
                hint="move the part, or move the hole; a cutout is not reclaimable "
                "area",
            )

    for index, entry in enumerate(placed):
        if not entry.fixed:
            continue
        for other in placed[index + 1 :]:
            if not other.fixed or not _overlap(entry.box, other.box):
                continue
            report.error(
                "fixed-courtyards-overlap",
                f"{entry.refdes} and {other.refdes} are both fixed, and their "
                "courtyards overlap",
                loc=netlist.mech_loc("placement", entry.refdes),
                path=netlist.mech_path("placement", entry.refdes, "fixed"),
                hint="two parts cannot occupy the same board area; the courtyard is "
                "the manufacturer's statement of the room each one needs",
            )


def _overlap(a: Box, b: Box) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _check_allowed_sets(
    netlist: Netlist, frame: BoardFrame, extents: dict[str, Extent], report: Report
) -> None:
    """An ``edge`` or ``region`` whose allowed set holds nothing is a dead end."""
    placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        if entry.level == "fixed" or refdes not in netlist.components:
            continue
        if refdes not in placement.unsatisfied:
            continue
        report.error(
            "placement-set-empty",
            f"{refdes} has nowhere to go: no position in the {entry.level} its "
            "source allows fits inside the board with the parts already fixed there",
            loc=netlist.mech_loc("placement", refdes),
            path=netlist.mech_path("placement", refdes, entry.level),
            hint="widen the allowed set, check whether it falls outside the outline "
            "or inside a cutout, or move whatever is fixed in the way",
        )


# ---------------------------------------------------------------------------
# relative intent against the anchors
# ---------------------------------------------------------------------------


def _check_relative_intents(
    netlist: Netlist, frame: BoardFrame, extents: dict[str, Extent], report: Report
) -> None:
    """Report a `max_distance` that the anchors already make impossible.

    Conservative in one direction only. The allowed set of each member is
    over-approximated by a box, so the distance computed between two boxes is a
    *lower* bound on how close the parts can ever get. A complaint therefore means
    the constraint really cannot hold; silence means nothing either way, which is
    the honest thing for interval reasoning to say.
    """
    boxes = {
        refdes: _allowed_box(entry, frame, extents.get(refdes))
        for refdes, entry in netlist.placement.items()
    }
    for constraint in netlist.constraints:
        if constraint.kind != "max_distance":
            continue
        limit = getattr(constraint.constraint, "mm", None)
        if limit is None:
            continue
        members = [m for m in constraint.members if boxes.get(m) is not None]
        if len(members) < 2:
            continue
        for index, first in enumerate(members):
            for second in members[index + 1 :]:
                gap = _box_distance(boxes[first], boxes[second])  # type: ignore[arg-type]
                if gap <= limit + _TOLERANCE:
                    continue
                report.warning(
                    "constraint-unreachable",
                    f"{first} and {second} are asked to stay within {limit} mm of "
                    f"each other, but where the source pins them they can never be "
                    f"closer than about {gap:.1f} mm",
                    loc=constraint.loc,
                    path=constraint.source_path,
                    hint="relax the distance, or loosen one of the two mechanical "
                    "placements; this is a bound rather than an exact answer, so it "
                    "only ever complains when the constraint is genuinely impossible",
                )


def _allowed_box(
    entry: MechPlacement, frame: BoardFrame, extent: Extent | None
) -> Box | None:
    """Every position the source allows a part's origin to take, as one box."""
    if entry.fixed is not None:
        x, y = frame.to_kicad((entry.fixed.x, entry.fixed.y))
        return (x, y, x, y)
    if entry.region is not None:
        x1, y1, x2, y2 = entry.region.bounds
        corners = [frame.to_kicad((x1, y1)), frame.to_kicad((x2, y2))]
        return (
            min(c[0] for c in corners), min(c[1] for c in corners),
            max(c[0] for c in corners), max(c[1] for c in corners),
        )
    edge = entry.edge
    if edge is None:  # pragma: no cover - the model allows nothing else
        return None
    along_x = edge.side in ("north", "south")
    low, high = (
        (frame.source_min[0], frame.source_max[0])
        if along_x
        else (frame.source_min[1], frame.source_max[1])
    )
    if edge.offset_range is not None:
        low, high = max(low, edge.offset_range[0]), min(high, edge.offset_range[1])
    depth = max(extent.width, extent.height) if extent else 2.0
    if along_x:
        ends = [frame.to_kicad((low, 0.0))[0], frame.to_kicad((high, 0.0))[0]]
        anchor = frame.to_kicad(
            (0.0, frame.source_max[1] if edge.side == "north" else frame.source_min[1])
        )[1]
        return (min(ends), anchor - depth, max(ends), anchor + depth)
    ends = [frame.to_kicad((0.0, low))[1], frame.to_kicad((0.0, high))[1]]
    anchor = frame.to_kicad(
        (frame.source_max[0] if edge.side == "east" else frame.source_min[0], 0.0)
    )[0]
    return (anchor - depth, min(ends), anchor + depth, max(ends))


def _box_distance(a: Box, b: Box) -> float:
    """How close two boxes can possibly be. Zero when they overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)

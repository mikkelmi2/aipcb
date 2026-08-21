"""Placing footprints on the board from the design's placement intent.

The source never says where a part goes. It says what belongs near what and why --
a decoupling capacitor is `for:` an IC, a `group` constraint holds a differential
pair's series resistors together, a `max_distance` bounds a loop. Placement's job
is to turn those relationships into coordinates.

The algorithm is deliberately simple and completely deterministic:

1. **Cluster.** Components joined by a placement constraint, or by a ``for:``
   reference, are gathered into one cluster with a union-find. This is where intent
   becomes structure: nothing else in the pipeline knows that a capacitor belongs
   beside its chip.
2. **Pack each cluster** into a compact grid of its own, ordered by reference
   designator so the result never depends on dictionary iteration order.
3. **Shelf-pack the clusters** into the usable board area, largest first, so big
   parts get placed while there is still room around them.

Three levels sit above that, and they outrank it (ADR 0008). A ``fixed`` component
is mechanical law: it is placed first, exactly where the source says, and nothing
moves it. An ``edge`` or ``region`` component is placed by this module but only from
the set the source allows. Everything else is relative intent, and a cluster that
contains an anchor packs *around* the anchor rather than being shelf-packed
somewhere else -- which is how a decoupling group follows its connector to the
board edge without any new mechanism.

The board is a polygon, not a width and a height. Candidate positions are tested
against the real outline less its cutouts, so a part cannot be packed into the
missing corner of an L-shaped board or over the hole a flex tail comes through.

It is not an optimiser and does not pretend to be. It produces a legal, repeatable
starting point that respects the stated intent; refining it is the job of a human
in KiCad, whose work M6 then preserves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aipcb.compile.frame import BoardFrame, frame_for
from aipcb.diagnostics import Report
from aipcb.kicad.footprints import Extent, FootprintNotFound, footprint_extent, resolve_footprint
from aipcb.model.layout import BoardOutline, Layout
from aipcb.model.mech import MechPlacement
from aipcb.netlist import Netlist

__all__ = [
    "BoardPlacement",
    "Placed",
    "component_extents",
    "courtyard_box",
    "plan_placement",
    "usable_area",
]

#: Space kept between neighbouring parts, measured between courtyards. Wide enough
#: that a router has somewhere to go -- the difference between a placement that can
#: be routed and one that merely fits -- and wide enough that reference designators,
#: which are drawn outside the courtyard, do not collide on the silkscreen.
PART_SPACING = 2.5
CLUSTER_SPACING = 3.0
DEFAULT_GRID = 0.5

#: How far a packed cluster is kept from an anchored part's courtyard. The same
#: figure the shelf packer leaves between neighbours, and for the same two reasons:
#: a router needs somewhere to go, and a reference designator is drawn outside the
#: courtyard and has to land somewhere that is not another part's silkscreen.
ANCHOR_SPACING = PART_SPACING

#: How many rings out from an anchor a cluster is tried before it gives up and
#: joins the shelf packer. Each ring is one block-width further out, so eight is
#: most of a board.
ANCHOR_RINGS = 8

#: Where a cluster is tried relative to its anchor, nearest first. Right and below
#: come first because that is where a footprint's own body extends from its origin,
#: so those placements read most naturally on the finished board.
_ANCHOR_DIRECTIONS = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1))


@dataclass(frozen=True, slots=True)
class Placed:
    """Where one footprint ends up."""

    refdes: str
    x: float
    y: float
    rotation: float = 0.0
    side: str = "front"

    @property
    def layer(self) -> str:
        return "F.Cu" if self.side == "front" else "B.Cu"


@dataclass(slots=True)
class BoardPlacement:
    """The result of placing every footprint."""

    positions: dict[str, Placed] = field(default_factory=dict)
    width: float = 0.0
    height: float = 0.0
    origin: tuple[float, float] = (0.0, 0.0)
    unsatisfied: list[str] = field(default_factory=list)
    """Mechanically constrained parts whose allowed set turned out to hold nothing.

    They are still placed -- by the packer, wherever there was room -- because a
    board with a part in the wrong place is more use than a board with a part
    missing. The validator turns this into the error it is.
    """

    def __getitem__(self, refdes: str) -> Placed:
        return self.positions[refdes]


# ---------------------------------------------------------------------------
# board area
# ---------------------------------------------------------------------------


def component_extents(netlist: Netlist) -> tuple[dict[str, Extent], list[str]]:
    """Every component's courtyard, and the reference designators that have none.

    Best effort by design: the caller decides what a missing footprint means. The
    placer can carry on with a default box, while the mechanical validator cannot
    say anything useful about courtyards it has not got and stays quiet instead.
    """
    extents: dict[str, Extent] = {}
    missing: list[str] = []
    for component in netlist.sorted_components():
        if component.part is None:
            missing.append(component.refdes)
            continue
        try:
            extents[component.refdes] = footprint_extent(
                resolve_footprint(component.part.footprint)
            )
        except FootprintNotFound:
            missing.append(component.refdes)
    return extents, missing


def usable_area(outline: BoardOutline | None, margin: float) -> tuple[float, float]:
    """The interior a placer may use, in millimetres."""
    if outline is None:
        return (100.0, 80.0)
    if outline.shape == "rect":
        width = (outline.width_mm or 100.0) - 2 * margin
        height = (outline.height_mm or 80.0) - 2 * margin
        return (max(width, 1.0), max(height, 1.0))
    xs = [p[0] for p in outline.points_mm]
    ys = [p[1] for p in outline.points_mm]
    return (
        max(max(xs) - min(xs) - 2 * margin, 1.0),
        max(max(ys) - min(ys) - 2 * margin, 1.0),
    )


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


class _Union:
    """Union-find over reference designators."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Join toward the lexicographically smaller root so the result does not
            # depend on the order the pairs arrive in.
            low, high = sorted((ra, rb))
            self._parent[high] = low


def _clusters(netlist: Netlist) -> list[list[str]]:
    """Group components that intent says belong together."""
    union = _Union()
    by_path = {c.path_text: c.refdes for c in netlist.components.values()}
    for refdes in netlist.components:
        union.add(refdes)

    def resolve(name: str) -> str | None:
        if name in netlist.components:
            return name
        return by_path.get(name)

    for constraint in netlist.constraints:
        if constraint.kind == "keep_apart":
            continue  # a reason to separate, not to cluster
        members = [r for m in constraint.members if (r := resolve(m)) is not None]
        for other in members[1:]:
            union.join(members[0], other)

    for component in netlist.components.values():
        if not component.for_ref:
            continue
        scope = component.hier[:-1]
        target = resolve(".".join((*scope, component.for_ref))) or resolve(
            component.for_ref
        )
        if target is not None:
            union.join(component.refdes, target)

    grouped: dict[str, list[str]] = {}
    for refdes in sorted(netlist.components):
        grouped.setdefault(union.find(refdes), []).append(refdes)
    return [sorted(members) for _, members in sorted(grouped.items())]


# ---------------------------------------------------------------------------
# packing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    """A cluster reduced to a box, with its members' offsets inside it."""

    key: str
    width: float
    height: float
    members: list[tuple[str, float, float]]


def _pack_cluster(
    members: list[str], extents: dict[str, Extent], grid: float
) -> _Block:
    """Arrange one cluster's members into a compact grid."""
    columns = 1
    while columns * columns < len(members):
        columns += 1

    cell_w = max((extents[r].width for r in members), default=1.0) + PART_SPACING
    cell_h = max((extents[r].height for r in members), default=1.0) + PART_SPACING

    placed: list[tuple[str, float, float]] = []
    for index, refdes in enumerate(members):
        row, col = divmod(index, columns)
        placed.append((refdes, _snap(col * cell_w, grid), _snap(row * cell_h, grid)))

    rows = -(-len(members) // columns)
    return _Block(members[0], columns * cell_w, rows * cell_h, placed)


def _snap(value: float, grid: float) -> float:
    return round(round(value / grid) * grid, 4)


def _snap_inward(value: float, grid: float, inward: float) -> float:
    """Snap to the grid, never outward.

    A part sitting against the board edge is exactly on the boundary of what is
    allowed, so rounding it to the nearest grid line is a coin toss between legal
    and half a grid step over the edge. Rounding toward the board is not.
    """
    steps = value / grid
    snapped = math.ceil(steps - 1e-9) if inward > 0 else math.floor(steps + 1e-9)
    return round(snapped * grid, 4)


def plan_placement(
    netlist: Netlist,
    *,
    report: Report | None = None,
    extents: dict[str, Extent] | None = None,
    frame: BoardFrame | None = None,
) -> BoardPlacement:
    """Place every component, honouring the design's placement intent."""
    layout: Layout | None = netlist.layout
    margin = layout.placement.margin_mm if layout else 2.0
    grid = layout.placement.grid_mm if layout else DEFAULT_GRID
    origin = layout.origin_mm if layout else (100.0, 100.0)
    frame = frame if frame is not None else frame_for(netlist)
    width, height = _usable_span(frame, margin)

    extents = extents or {}
    sides = _sides(netlist, report)
    placement = BoardPlacement(width=width, height=height, origin=origin)
    area = _Area(frame, margin, extents)

    # 1. Mechanical law. `fixed` parts go exactly where the source says, before
    #    anything else exists to be in their way, and become anchors.
    anchors = _place_fixed(netlist, frame, placement, sides, area)
    # 2. Partially constrained parts choose a position, but only from their set.
    anchors |= _place_constrained(netlist, frame, grid, placement, sides, area, report)

    # Components pinned to a region go first and are then out of the way: the
    # source has said where they belong, and that is not the packer's business to
    # second-guess. Everything else shares what is left.
    pinned = anchors | _place_regions(
        netlist, extents, origin, grid, placement, sides, report
    )

    blocks: list[_Block] = []
    for cluster in _clusters(netlist):
        free = [m for m in cluster if m not in pinned]
        if not free:
            continue
        block = _pack_cluster(free, _with_defaults(extents, free), grid)
        held = [m for m in cluster if m in anchors]
        # 3. A cluster with an anchor in it packs around the anchor. This is the
        #    whole of "relative intents pull movable parts toward their anchors":
        #    the union-find has already put a decoupling group in the same cluster
        #    as its chip, so an anchored cluster is a cluster with a fixed origin.
        if held and _place_beside_anchor(
            block, held, placement, extents, area, grid, sides
        ):
            continue
        blocks.append(block)

    # Tallest first: a shelf packer that meets a big part late has nowhere to put it.
    blocks.sort(key=lambda b: (-b.height, -b.width, b.key))

    cursor_x = 0.0
    cursor_y = 0.0
    shelf_height = 0.0
    overflow = False
    used_x = 0.0
    used_y = 0.0

    for block in blocks:
        if cursor_x > 0 and cursor_x + block.width > width:
            cursor_x = 0.0
            cursor_y += shelf_height + CLUSTER_SPACING
            shelf_height = 0.0
        if cursor_y + block.height > height:
            overflow = True
        corner = _shelf_position(
            block, cursor_x, cursor_y, width, height, origin, margin, grid,
            extents, area, sides,
        )
        if corner is None:
            corner = (origin[0] + margin + cursor_x, origin[1] + margin + cursor_y)
            overflow = True
        _put_block(block, corner, placement, extents, area, grid, sides)
        cursor_x = corner[0] - origin[0] - margin + block.width + CLUSTER_SPACING
        cursor_y = corner[1] - origin[1] - margin
        shelf_height = max(shelf_height, block.height)
        used_x = max(used_x, cursor_x - CLUSTER_SPACING)
        used_y = max(used_y, cursor_y + block.height)

    if overflow and report is not None:
        need_w = round(used_x + 2 * margin, 1)
        need_h = round(used_y + 2 * margin, 1)
        report.warning(
            "placement-overflow",
            f"the components do not fit inside the board outline: they need about "
            f"{need_w} x {need_h} mm, and the outline gives "
            f"{width + 2 * margin:.1f} x {height + 2 * margin:.1f} mm",
            hint=f"set the board outline to at least {need_w} x {need_h} mm, or reduce "
            "`layout.placement.margin_mm`; the parts have been placed past the edge "
            "so the board is still inspectable",
        )
    return placement


# ---------------------------------------------------------------------------
# the usable area
# ---------------------------------------------------------------------------

Box = tuple[float, float, float, float]


def _usable_span(frame: BoardFrame | None, margin: float) -> tuple[float, float]:
    """The interior's bounding size, which is what the shelf packer walks."""
    if frame is None:
        return (100.0, 80.0)
    return (
        max(frame.width - 2 * margin, 1.0),
        max(frame.height - 2 * margin, 1.0),
    )


def courtyard_box(extent: Extent, x: float, y: float, rotation: float) -> Box:
    """Where a footprint's courtyard lands on the board, as an axis-aligned box.

    KiCad turns a footprint counter-clockwise *as drawn*, and its files have Y
    pointing down, so the rotation is the mirror of the textbook one -- the same
    transform the router uses to find a rotated part's pads.
    """
    corners: tuple[tuple[float, float], ...] = (
        (extent.min_x, extent.min_y), (extent.max_x, extent.min_y),
        (extent.max_x, extent.max_y), (extent.min_x, extent.max_y),
    )
    if rotation:
        theta = math.radians(rotation)
        cos, sin = math.cos(theta), math.sin(theta)
        corners = tuple(
            (cx * cos + cy * sin, -cx * sin + cy * cos) for cx, cy in corners
        )
    xs = [x + cx for cx, _ in corners]
    ys = [y + cy for _, cy in corners]
    return (min(xs), min(ys), max(xs), max(ys))


class _Area:
    """Where a footprint may go: inside the board, outside everything anchored.

    The containment test engages only when it can say something the shelf packer's
    own arithmetic does not. On a plain rectangle with nothing anchored, the packer
    already knows where the edges are, and re-deriving that from the polygon would
    change nothing but floating-point luck -- so a rectangular board with no
    cutouts and no anchors packs exactly as it did before M9, byte for byte.
    """

    __slots__ = ("_extents", "margin", "occupied", "rectangular", "usable")

    def __init__(
        self, frame: BoardFrame | None, margin: float, extents: dict[str, Extent]
    ) -> None:
        self.margin = margin
        self.usable = frame.usable(margin) if frame is not None else None
        self.rectangular = frame is None or _is_bounding_box(frame)
        self.occupied: list[Box] = []
        self._extents = extents

    def occupy(self, box: Box) -> None:
        self.occupied.append(box)

    def fits(self, boxes: list[Box]) -> bool:
        """Whether every box is inside the board and clear of what is anchored."""
        if self.rectangular and not self.occupied:
            return True
        for box in boxes:
            if any(_overlaps(box, taken, ANCHOR_SPACING) for taken in self.occupied):
                return False
        if self.usable is None or self.rectangular:
            return True
        from shapely.geometry import box as shapely_box

        return all(self.usable.covers(shapely_box(*b).buffer(-1e-9)) for b in boxes)

    def box_for(self, refdes: str, x: float, y: float, rotation: float) -> Box:
        extent = self._extents.get(refdes) or Extent(-1.0, -1.0, 1.0, 1.0)
        return courtyard_box(extent, x, y, rotation)


def _is_bounding_box(frame: BoardFrame) -> bool:
    """Whether the outline is its own bounding rectangle, with nothing cut out of it."""
    if frame.cutouts:
        return False
    points = frame.polygon()
    if len(points) != 4:
        return False
    xs = {round(p[0], 6) for p in points}
    ys = {round(p[1], 6) for p in points}
    return len(xs) == 2 and len(ys) == 2


def _overlaps(a: Box, b: Box, gap: float) -> bool:
    return not (
        a[2] + gap <= b[0] or b[2] + gap <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


# ---------------------------------------------------------------------------
# placing a block
# ---------------------------------------------------------------------------


def _block_boxes(
    block: _Block,
    corner: tuple[float, float],
    extents: dict[str, Extent],
    grid: float,
    sides: dict[str, str],
) -> list[tuple[str, float, float, Box]]:
    """Where each member of a block would land, and the courtyard it would occupy."""
    out: list[tuple[str, float, float, Box]] = []
    for refdes, dx, dy in block.members:
        extent = extents.get(refdes)
        # A footprint's origin is not its centre, so shift by the extent to keep
        # the whole body inside the area rather than just the anchor point.
        offset_x = -extent.min_x if extent else 0.0
        offset_y = -extent.min_y if extent else 0.0
        x = _snap(corner[0] + dx + offset_x, grid)
        y = _snap(corner[1] + dy + offset_y, grid)
        out.append(
            (refdes, x, y, courtyard_box(extent or Extent(-1.0, -1.0, 1.0, 1.0), x, y, 0.0))
        )
    return out


def _put_block(
    block: _Block,
    corner: tuple[float, float],
    placement: BoardPlacement,
    extents: dict[str, Extent],
    area: _Area,
    grid: float,
    sides: dict[str, str],
) -> None:
    for refdes, x, y, box in _block_boxes(block, corner, extents, grid, sides):
        placement.positions[refdes] = Placed(
            refdes, x, y, rotation=0.0, side=sides.get(refdes, "front")
        )
        if not area.rectangular or area.occupied:
            area.occupy(box)


#: How far a block slides when the natural position is taken, in millimetres. Fine
#: enough to find a gap between two anchored parts, coarse enough that scanning a
#: whole board is a few hundred containment tests rather than a few hundred
#: thousand.
_SLIDE_STEP = 2.0


def _shelf_position(
    block: _Block,
    cursor_x: float,
    cursor_y: float,
    width: float,
    height: float,
    origin: tuple[float, float],
    margin: float,
    grid: float,
    extents: dict[str, Extent],
    area: _Area,
    sides: dict[str, str],
) -> tuple[float, float] | None:
    """Where this block goes, sliding past whatever the board's shape puts in the way.

    The natural cursor position is tried first, so nothing moves on a board that has
    no reason to move it -- which is every rectangular board with nothing anchored,
    and is what keeps M9 from re-flowing the existing examples. On a board with a
    cutout, a missing corner or a fixed connector in the way, the block slides right
    along the shelf and then down, in that order, which is what a shelf packer does
    anyway; the only difference is that "in the way" now means the real polygon.
    """
    step = max(_SLIDE_STEP, grid)
    y = cursor_y
    while y + block.height <= max(height, block.height) + 1e-9:
        x = cursor_x
        while x + block.width <= max(width, block.width) + 1e-9:
            corner = (origin[0] + margin + x, origin[1] + margin + y)
            boxes = [b for _, _, _, b in _block_boxes(block, corner, extents, grid, sides)]
            if area.fits(boxes):
                return corner
            x += step
        y += step
        cursor_x = 0.0
    return None


# ---------------------------------------------------------------------------
# mechanical placement (ADR 0008)
# ---------------------------------------------------------------------------


def _place_fixed(
    netlist: Netlist,
    frame: BoardFrame | None,
    placement: BoardPlacement,
    sides: dict[str, str],
    area: _Area,
) -> set[str]:
    """Put every ``fixed`` component exactly where the source says. Nothing moves it.

    Not snapped to ``grid_mm``. A grid is a convenience for parts whose position
    nobody cares about; a connector aligned to an enclosure opening is not one of
    them, and rounding it to the nearest half millimetre would be the tool quietly
    disagreeing with the mechanical drawing.
    """
    anchors: set[str] = set()
    if frame is None:
        return anchors
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        if entry.fixed is None or refdes not in netlist.components:
            continue
        x, y = frame.to_kicad((entry.fixed.x, entry.fixed.y))
        rotation = frame.rotation_to_kicad(entry.fixed.rot)
        placement.positions[refdes] = Placed(
            refdes, round(x, 4), round(y, 4), rotation=rotation,
            side=sides.get(refdes, "front"),
        )
        area.occupy(area.box_for(refdes, x, y, rotation))
        anchors.add(refdes)
    return anchors


def _place_constrained(
    netlist: Netlist,
    frame: BoardFrame | None,
    grid: float,
    placement: BoardPlacement,
    sides: dict[str, str],
    area: _Area,
    report: Report | None,
) -> set[str]:
    """Place ``edge`` and ``region`` components, projected into their allowed set."""
    anchors: set[str] = set()
    if frame is None:
        return anchors
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        if entry.level == "fixed" or refdes not in netlist.components:
            continue
        chosen = (
            _choose_on_edge(refdes, entry, frame, grid, area)
            if entry.edge is not None
            else _choose_in_region(refdes, entry, frame, grid, area)
        )
        if chosen is None:
            # Recorded rather than forced. The part falls through to the packer, so
            # the board is still inspectable, and `aipcb validate` reports the
            # conflict against the line that wrote it.
            placement.unsatisfied.append(refdes)
            if report is not None:
                report.warning(
                    "placement-set-unusable",
                    f"{refdes} could not be placed anywhere in the "
                    f"{entry.level} its source allows",
                    path=netlist.mech_path("placement", refdes, entry.level),
                    hint="widen the allowed set, or check whether a fixed part or a "
                    "cutout has taken all of it",
                )
            continue
        x, y, rotation = chosen
        placement.positions[refdes] = Placed(
            refdes, _snap(x, grid), _snap(y, grid), rotation=rotation,
            side=sides.get(refdes, "front"),
        )
        area.occupy(area.box_for(refdes, x, y, rotation))
        anchors.add(refdes)
    return anchors


def _choose_in_region(
    refdes: str, entry: MechPlacement, frame: BoardFrame, grid: float, area: _Area
) -> tuple[float, float, float] | None:
    """The first grid position inside the region whose courtyard fits, centre first."""
    assert entry.region is not None
    x1, y1, x2, y2 = entry.region.bounds
    corners = [frame.to_kicad((x1, y1)), frame.to_kicad((x2, y2))]
    left, right = min(c[0] for c in corners), max(c[0] for c in corners)
    top, bottom = min(c[1] for c in corners), max(c[1] for c in corners)
    rotation = frame.rotation_to_kicad(entry.region.rot or 0.0)

    step = max(grid, 0.1)
    centre = ((left + right) / 2, (top + bottom) / 2)
    candidates = sorted(
        (
            (left + i * step, top + j * step)
            for i in range(int((right - left) / step) + 1)
            for j in range(int((bottom - top) / step) + 1)
        ),
        key=lambda p: (round(math.dist(p, centre), 6), p),
    )
    for raw_x, raw_y in candidates:
        # Snapped before it is tested, not after: a position that passes the fit
        # test and is then nudged onto the grid is a position nobody checked.
        x, y = _snap(raw_x, grid), _snap(raw_y, grid)
        box = area.box_for(refdes, x, y, rotation)
        if box[0] < left - 1e-9 or box[2] > right + 1e-9:
            continue
        if box[1] < top - 1e-9 or box[3] > bottom + 1e-9:
            continue
        if area.fits([box]):
            return (x, y, rotation)
    return None


def _choose_on_edge(
    refdes: str, entry: MechPlacement, frame: BoardFrame, grid: float, area: _Area
) -> tuple[float, float, float] | None:
    """Sit the part against one board edge, within the span the source allows.

    On a rectangle the edge is where you would expect. On anything else it is found
    by asking the real outline how far it reaches at the chosen offset, so a part on
    the north edge of an L-shaped board sits against the actual boundary above it
    rather than against the bounding box's.
    """
    assert entry.edge is not None
    edge = entry.edge
    rotation = frame.rotation_to_kicad(edge.rot if edge.rot is not None else 0.0)
    along_x = edge.side in ("north", "south")
    low, high = (
        (frame.source_min[0], frame.source_max[0])
        if along_x
        else (frame.source_min[1], frame.source_max[1])
    )
    if edge.offset_range is not None:
        low, high = max(low, edge.offset_range[0]), min(high, edge.offset_range[1])
    if high <= low:
        return None

    step = max(grid, 0.1)
    middle = (low + high) / 2
    offsets = sorted(
        (low + i * step for i in range(int((high - low) / step) + 1)),
        key=lambda o: (round(abs(o - middle), 6), o),
    )
    for offset in offsets:
        anchor = _edge_anchor(frame, edge.side, offset, along_x, area.margin)
        if anchor is None:
            continue
        box = area.box_for(refdes, 0.0, 0.0, rotation)
        if along_x:
            x = anchor[0]
            y = anchor[1] - box[1] if edge.side == "north" else anchor[1] - box[3]
        else:
            y = anchor[1]
            x = anchor[0] - box[2] if edge.side == "east" else anchor[0] - box[0]
        # KiCad's Y points down, so "into the board" from the north edge is +y.
        inward = {"north": 1.0, "south": -1.0, "east": -1.0, "west": 1.0}[edge.side]
        if along_x:
            x, y = _snap(x, grid), _snap_inward(y, grid, inward)
        else:
            x, y = _snap_inward(x, grid, inward), _snap(y, grid)
        placed = area.box_for(refdes, x, y, rotation)
        if area.fits([placed]):
            return (x, y, rotation)
    return None


def _edge_anchor(
    frame: BoardFrame, side: str, offset: float, along_x: bool, margin: float
) -> tuple[float, float] | None:
    """Where the board's real boundary is, at one offset along one edge.

    ``north`` is the source frame's +y side, which is the top of the board as KiCad
    draws it. The answer comes from intersecting the usable area with a line at the
    offset, so a concave outline gives the boundary it actually has there -- a part
    on the north edge of an L-shaped board sits against the boundary that is
    actually above it, not against the bounding box's.

    The usable area already has ``margin_mm`` taken off it, so "against the edge"
    means against the line the rest of the placer respects. Anything else would put
    an edge-constrained part closer to the edge than the parts around it, which is
    not what a margin is for.
    """
    from shapely.geometry import LineString

    usable = frame.usable(margin)
    if usable.is_empty:
        return None
    min_x, min_y, max_x, max_y = usable.bounds
    if along_x:
        x = frame.to_kicad((offset, frame.source_min[1]))[0]
        cut = LineString([(x, min_y - 1.0), (x, max_y + 1.0)])
    else:
        y = frame.to_kicad((frame.source_min[0], offset))[1]
        cut = LineString([(min_x - 1.0, y), (max_x + 1.0, y)])
    inside = cut.intersection(usable)
    if inside.is_empty:
        return None
    points = [c for geom in getattr(inside, "geoms", [inside]) for c in geom.coords]
    if not points:
        return None
    # North is the smallest KiCad y, because KiCad's Y points down. Getting this
    # backwards is exactly the mirrored-board bug the frame module exists to
    # prevent, so it is stated once, here.
    if side == "north":
        chosen = min(points, key=lambda p: (p[1], p[0]))
    elif side == "south":
        chosen = max(points, key=lambda p: (p[1], p[0]))
    elif side == "east":
        chosen = max(points, key=lambda p: (p[0], p[1]))
    else:
        chosen = min(points, key=lambda p: (p[0], p[1]))
    return (float(chosen[0]), float(chosen[1]))


def _place_beside_anchor(
    block: _Block,
    anchors: list[str],
    placement: BoardPlacement,
    extents: dict[str, Extent],
    area: _Area,
    grid: float,
    sides: dict[str, str],
) -> bool:
    """Try to pack a cluster next to the anchors it shares a cluster with.

    Deterministic and bounded: eight directions, tried nearest first, each pushed
    outward a block at a time. If none of them has room the cluster falls back to
    the shelf packer, which is no worse than where it would have gone anyway.
    """
    held = [placement.positions[a] for a in anchors if a in placement.positions]
    if not held:
        return False
    boxes = [
        courtyard_box(
            extents.get(p.refdes) or Extent(-1.0, -1.0, 1.0, 1.0), p.x, p.y, p.rotation
        )
        for p in held
    ]
    centre = (
        sum(b[0] + b[2] for b in boxes) / (2 * len(boxes)),
        sum(b[1] + b[3] for b in boxes) / (2 * len(boxes)),
    )
    reach_x = max(b[2] - b[0] for b in boxes) / 2 + ANCHOR_SPACING
    reach_y = max(b[3] - b[1] for b in boxes) / 2 + ANCHOR_SPACING

    for ring in range(1, ANCHOR_RINGS + 1):
        for dx, dy in _ANCHOR_DIRECTIONS:
            x = centre[0] - block.width / 2 + dx * (reach_x + ring * block.width / 2)
            y = centre[1] - block.height / 2 + dy * (reach_y + ring * block.height / 2)
            corner = (_snap(x, grid), _snap(y, grid))
            candidate = [
                b for _, _, _, b in _block_boxes(block, corner, extents, grid, sides)
            ]
            if area.fits(candidate):
                _put_block(block, corner, placement, extents, area, grid, sides)
                return True
    return False


def _place_regions(
    netlist: Netlist,
    extents: dict[str, Extent],
    origin: tuple[float, float],
    grid: float,
    placement: BoardPlacement,
    sides: dict[str, str],
    report: Report | None,
) -> set[str]:
    """Honour ``region_mm`` placement rules. Returns the components they pinned.

    A region is given relative to the board origin, so a design can say "this
    connector belongs on the left edge" without knowing where the board sits in
    KiCad's coordinate space.
    """
    layout = netlist.layout
    if layout is None:
        return set()

    by_path = {c.path_text: c.refdes for c in netlist.components.values()}
    pinned: set[str] = set()

    for index, rule in enumerate(layout.placement.rules):
        if rule.region_mm is None:
            continue
        x1, y1, x2, y2 = rule.region_mm
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)

        members = [
            refdes
            for member in rule.members
            if (refdes := (member if member in netlist.components else by_path.get(member)))
            is not None
        ]
        if not members:
            continue

        block = _pack_cluster(sorted(members), _with_defaults(extents, members), grid)
        if report is not None and (
            block.width > right - left or block.height > bottom - top
        ):
            report.warning(
                "region-too-small",
                f"the region for {', '.join(sorted(members))} is "
                f"{right - left:.1f} x {bottom - top:.1f} mm, but they need about "
                f"{block.width:.1f} x {block.height:.1f} mm",
                path=("layout", "placement", "rules", index, "region_mm"),
                hint="enlarge the region, or move some components out of the rule",
            )

        for refdes, dx, dy in block.members:
            extent = extents.get(refdes)
            offset_x = -extent.min_x if extent else 0.0
            offset_y = -extent.min_y if extent else 0.0
            placement.positions[refdes] = Placed(
                refdes,
                _snap(origin[0] + left + dx + offset_x, grid),
                _snap(origin[1] + top + dy + offset_y, grid),
                rotation=rule.orientation_deg or 0.0,
                side=sides.get(refdes, "front"),
            )
            pinned.add(refdes)

    return pinned


def _with_defaults(extents: dict[str, Extent], members: list[str]) -> dict[str, Extent]:
    fallback = Extent(-1.0, -1.0, 1.0, 1.0)
    return {r: extents.get(r, fallback) for r in members}


def _sides(netlist: Netlist, report: Report | None) -> dict[str, str]:
    """Which side each component goes on, reporting the case we cannot honour yet."""
    sides: dict[str, str] = {}
    for refdes in sorted(netlist.placement):
        entry = netlist.placement[refdes]
        if entry.fixed is None or entry.fixed.side != "back":
            continue
        if report is not None:
            report.warning(
                "back-side-placement-unsupported",
                f"{refdes} is fixed to the back, which this milestone does not "
                "implement; it has been placed on the front",
                path=("placement", refdes, "fixed", "side"),
                hint="mirroring a footprint means swapping every F./B. layer pair, "
                "and is deferred rather than approximated",
            )
    layout = netlist.layout
    if layout is None:
        return sides
    by_path = {c.path_text: c.refdes for c in netlist.components.values()}
    for index, rule in enumerate(layout.placement.rules):
        if rule.side is None:
            continue
        for member in rule.members:
            found = member if member in netlist.components else by_path.get(member)
            if found is None:
                continue
            refdes = found
            if rule.side == "back" and report is not None:
                report.warning(
                    "back-side-placement-unsupported",
                    f"{refdes} asks to be placed on the back, which this milestone "
                    "does not implement; it has been placed on the front",
                    path=("layout", "placement", "rules", index, "side"),
                    hint="mirroring a footprint means swapping every F./B. layer "
                    "pair, and is deferred rather than approximated",
                )
                continue
            sides[refdes] = rule.side
    return sides

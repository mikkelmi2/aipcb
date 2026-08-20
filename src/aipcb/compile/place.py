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

It is not an optimiser and does not pretend to be. It produces a legal, repeatable
starting point that respects the stated intent; refining it is the job of a human
in KiCad, whose work M6 then preserves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aipcb.diagnostics import Report
from aipcb.kicad.footprints import Extent
from aipcb.model.layout import BoardOutline, Layout
from aipcb.netlist import Netlist

__all__ = ["BoardPlacement", "Placed", "plan_placement", "usable_area"]

#: Space kept between neighbouring parts, measured between courtyards. Wide enough
#: that a router has somewhere to go -- the difference between a placement that can
#: be routed and one that merely fits -- and wide enough that reference designators,
#: which are drawn outside the courtyard, do not collide on the silkscreen.
PART_SPACING = 2.5
CLUSTER_SPACING = 3.0
DEFAULT_GRID = 0.5


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

    def __getitem__(self, refdes: str) -> Placed:
        return self.positions[refdes]


# ---------------------------------------------------------------------------
# board area
# ---------------------------------------------------------------------------


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


def plan_placement(
    netlist: Netlist,
    *,
    report: Report | None = None,
    extents: dict[str, Extent] | None = None,
) -> BoardPlacement:
    """Place every component, honouring the design's placement intent."""
    layout: Layout | None = netlist.layout
    margin = layout.placement.margin_mm if layout else 2.0
    grid = layout.placement.grid_mm if layout else DEFAULT_GRID
    origin = layout.origin_mm if layout else (100.0, 100.0)
    width, height = usable_area(layout.outline if layout else None, margin)

    extents = extents or {}
    sides = _sides(netlist, report)
    placement = BoardPlacement(width=width, height=height, origin=origin)

    # Components pinned to a region go first and are then out of the way: the
    # source has said where they belong, and that is not the packer's business to
    # second-guess. Everything else shares what is left.
    pinned = _place_regions(netlist, extents, origin, grid, placement, sides, report)

    remaining = [
        free
        for cluster in _clusters(netlist)
        if (free := [m for m in cluster if m not in pinned])
    ]
    blocks = [
        _pack_cluster(members, _with_defaults(extents, members), grid)
        for members in remaining
    ]
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
        for refdes, dx, dy in block.members:
            extent = extents.get(refdes)
            # A footprint's origin is not its centre, so shift by the extent to keep
            # the whole body inside the area rather than just the anchor point.
            offset_x = -extent.min_x if extent else 0.0
            offset_y = -extent.min_y if extent else 0.0
            placement.positions[refdes] = Placed(
                refdes,
                _snap(origin[0] + margin + cursor_x + dx + offset_x, grid),
                _snap(origin[1] + margin + cursor_y + dy + offset_y, grid),
                rotation=0.0,
                side=sides.get(refdes, "front"),
            )
        cursor_x += block.width + CLUSTER_SPACING
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
            hint=f"set `layout.outline` to at least {need_w} x {need_h} mm, or reduce "
            "`layout.placement.margin_mm`; the parts have been placed past the edge "
            "so the board is still inspectable",
        )
    return placement


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
    """Read `side:` from placement rules, reporting the case we cannot honour yet."""
    sides: dict[str, str] = {}
    layout = netlist.layout
    if layout is None:
        return sides
    by_path = {c.path_text: c.refdes for c in netlist.components.values()}
    for index, rule in enumerate(layout.placement.rules):
        if rule.side is None:
            continue
        for member in rule.members:
            refdes = member if member in netlist.components else by_path.get(member)
            if refdes is None:
                continue
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

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Where things go on a schematic sheet, and why.

M2 placed symbols on a square grid in module order. That is a netlist with
coordinates: every fact the source knows about *what a component is for* -- its
role, the IC it serves, the module it came from -- was thrown away at the sheet
boundary, and a reviewer got it back only by reading net labels one at a time.

This module spends that information instead. The plan it produces is built in five
steps, each of which answers one question a draughtsman answers by hand:

1. **Which parts are satellites?** A decoupling cap is not a peer of the IC it
   serves; it belongs *at* it. ``role`` and ``for:`` say which parts these are, and
   :func:`_satellites` resolves them to a host.
2. **What are the blocks?** A module instance is one visual cluster, because that
   is what the source said it was. Everything else is its own block, carrying its
   satellites with it.
3. **Which way does the signal flow?** Ranking blocks by breadth-first distance
   from the connectors, over the *signal* nets only, puts inputs on the left,
   controllers in the middle and outputs on the right. Power and ground are
   excluded from that graph on purpose: they touch everything, and a graph in which
   everything is adjacent has no layers.
4. **What order within a rank?** The barycentre heuristic, swept both ways. This is
   the step that removes wire crossings, and it is the only reason the numbers in
   the M14 report move as far as they do.
5. **Where exactly?** Extents are measured from the symbols actually in use --
   including the room a power symbol or a net label needs -- so nothing overlaps
   and no stub lands on a neighbour's pin.

Nothing here decides *connectivity*. Connectivity is names, and names come from the
source (ADR 0003); this module only decides where those names are drawn. That
separation is what lets placement change this much without a single net moving.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from aipcb.compile.geometry import Point, place_direction, place_point
from aipcb.kicad.symbols import Symbol
from aipcb.netlist import ElabComponent, Netlist

__all__ = [
    "BLOCK_GAP",
    "COLUMN_GAP",
    "GRID",
    "Block",
    "Extent",
    "SheetPlan",
    "SymbolPlacement",
    "TextPlace",
    "component_extent",
    "dense_sides",
    "plan_sheet",
    "snap",
    "text_layout",
    "text_placement",
]

#: KiCad's connection grid. Everything we emit is snapped to it: an off-grid pin is
#: legal but unusable, because a human cannot draw a wire that meets it.
GRID = 1.27

#: How far a pin's stub runs before its label or power symbol.
STUB = 3.81

#: Room a power symbol needs beyond the point its pin sits on.
POWER_REACH = 5.08

#: Roughly how wide one character of a 1.27 mm label is. Measured off KiCad's own
#: renderer rather than guessed: a 12-character net name at this size plots 9.4 mm
#: wide, which is 0.78 mm per character. Rounded up, because a label that overlaps
#: its neighbour is worse than one with air around it.
LABEL_CHAR_W = 0.95

#: Height of one line of 1.27 mm text, with the leading a plot gives it.
TEXT_H = 1.9

#: One character of a 1.27 mm property (a reference designator, a value).
TEXT_CHAR_W = 0.85

#: A global label draws a box with a pointed end around its text, which reaches
#: further than the glyphs do. This is the difference, measured off a plot.
LABEL_BOX = 2.54

#: Gap between two blocks stacked in the same column.
BLOCK_GAP = 6.35

#: Gap between two columns of the flow.
COLUMN_GAP = 12.7

#: Gap between a host and the satellites hanging off it.
SATELLITE_GAP = 3.81

#: Gap between two satellites in the same slot.
SATELLITE_PITCH = 2.54

#: Sheet margin.
MARGIN = 20.32

#: Height of the band across the top of the sheet where the power flags live.
FLAG_BAND = 12.7

#: Net classes that carry no flow information: they touch everything, so an edge
#: through one says nothing about which side of the sheet a part belongs on.
POWER_CLASSES = frozenset({"power", "ground"})

#: Roles whose components belong *at* another component rather than beside it.
#: Each maps to the slot it occupies around its host.
SATELLITE_SLOTS: Mapping[str, str] = {
    "decoupling": "south",
    "bypass": "south",
    "bulk": "south",
    "pull_down": "south",
    "pull_up": "north",
    "crystal_load": "north",
    "series": "east",
    "ac_coupling": "east",
    "termination": "east",
    "filter": "east",
    "snubber": "east",
    "current_limit": "east",
    "esd": "east",
    "reverse_protection": "west",
    "sense": "east",
    "feedback": "north",
    "divider": "east",
}

#: Roles that put a component at the edge of the sheet, facing the flow.
EDGE_ROLES = frozenset({"connector", "edge_connector"})

#: Roles that belong in the middle of the flow, whatever the graph says.
CENTRE_ROLES = frozenset({"mcu", "regulator", "level_shifter", "oscillator"})


def snap(value: float, grid: float = GRID) -> float:
    """Round to the connection grid, without floating-point drift in the result."""
    return round(round(value / grid) * grid, 4)


# ---------------------------------------------------------------------------
# extents
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Extent:
    """How far a placed symbol reaches from its origin, in sheet millimetres.

    All four numbers are positive distances outward. They include the stub, and
    whatever sits on the end of it -- a power symbol's body or a net label's text --
    because a schematic is unreadable exactly when those collide.
    """

    left: float = 0.0
    right: float = 0.0
    up: float = 0.0
    down: float = 0.0

    @property
    def width(self) -> float:
        return self.left + self.right

    @property
    def height(self) -> float:
        return self.up + self.down

    def union(self, other: Extent, dx: float = 0.0, dy: float = 0.0) -> Extent:
        """Merge another extent whose origin sits at ``(dx, dy)`` relative to ours."""
        return Extent(
            left=max(self.left, other.left - dx),
            right=max(self.right, other.right + dx),
            up=max(self.up, other.up - dy),
            down=max(self.down, other.down + dy),
        )


def _body_extent(symbol: Symbol) -> Extent:
    """The symbol's own drawing, in library space, as distances from its origin.

    Read from the graphic primitives rather than inferred from the pins, because a
    part whose body is wider than its pin field -- a connector shell, an IC
    rectangle drawn past its pin rows -- would otherwise be measured too small and
    placed on top of its neighbour.
    """
    xs: list[float] = []
    ys: list[float] = []

    def collect(node: object) -> None:
        from aipcb.kicad.sexpr import SNode

        if not isinstance(node, SNode):
            return
        if node.name in ("xy", "start", "end", "mid", "center"):
            atoms = node.atoms()
            if len(atoms) >= 2:
                try:
                    xs.append(float(atoms[0].value))
                    ys.append(float(atoms[1].value))
                except ValueError:  # pragma: no cover - malformed library
                    pass
        for item in node.items:
            collect(item)

    for unit in symbol.node.children("symbol"):
        for shape in unit.items:
            collect(shape)

    if not xs:
        return Extent()
    # Library space is Y-up, the sheet is Y-down, so the highest y becomes `up`.
    return Extent(
        left=max(0.0, -min(xs)), right=max(0.0, max(xs)),
        up=max(0.0, max(ys)), down=max(0.0, -min(ys)),
    )


def _terminal_reach(net_name: str | None, power: bool) -> float:
    """How far past the stub end the thing on it reaches.

    A power symbol is a fixed shape. A label is text, and its length is the reason
    a sheet full of long bus names needs more room than one full of ``D0``.
    """
    if net_name is None:
        return 1.27  # a no-connect marker
    if power:
        return POWER_REACH
    return LABEL_CHAR_W * len(net_name) + LABEL_BOX


def _stub_lengths(
    component: ElabComponent, symbol: Symbol, rotation: float
) -> dict[str, float]:
    """How far each pin's stub runs before its label or power symbol.

    Almost always the same short length, which is what a schematic normally looks
    like. The exception is a side whose pins are closer together than a label is
    *tall*: there, equal stubs put one label on top of the next, and alternate pins
    run out one label-width further so they form the staircase a draughtsman draws by
    hand. The step is measured from the widest label actually on that side rather
    than assumed.

    The threshold was tuned by measurement, not chosen. Staggering every crowded side
    -- the obvious rule, and the first one implemented -- turned `pcie-sata`'s 396 mm
    of stub into 866 mm and removed no overlap that the label-on-a-crowded-side rule
    in :func:`dense_sides` had not already removed for free. On real parts a 2.54 mm
    pitch clears a 1.9 mm label, so this now fires rarely and costs nothing when it
    does not.
    """
    sides: dict[tuple[int, int], list[tuple[float, str]]] = {}
    for pin in symbol.pins:
        outward = place_direction(rotation, pin.outward_angle)
        key = (round(outward.x), round(outward.y))
        anchor = place_point(Point(0.0, 0.0), rotation, pin.x, pin.y)
        along = anchor.y if abs(key[0]) else anchor.x
        sides.setdefault(key, []).append((along, pin.number))

    lengths: dict[str, float] = {}
    for members in (sides[k] for k in sorted(sides)):
        members.sort()
        pitch = min(
            (abs(b[0] - a[0]) for a, b in pairwise(members)),
            default=float("inf"),
        )
        widest = max(
            (
                _terminal_reach(component.connections.get(number), False)
                for _, number in members
            ),
            default=0.0,
        )
        # Stagger only when the pins are closer together than the labels are tall.
        # Anything wider than that needs no staircase, and building one anyway is
        # what turns 400 mm of stub into 870 mm without removing a single overlap.
        stagger = len(members) >= 3 and pitch < TEXT_H + 0.63
        step = snap(widest + 1.27) if stagger else 0.0
        for index, (_, number) in enumerate(members):
            lengths[number] = STUB + (index % 2) * step
    return lengths


def dense_sides(
    symbol: Symbol, rotation: float
) -> frozenset[tuple[int, int]]:
    """Which sides of a symbol have pins too close together for a power symbol.

    A power symbol is a shape with its net name written beside it, and both are
    wider than a 2.54 mm pin pitch. Standing one on every pin of a six-way header
    puts three net names on top of three other net names -- which is worse than the
    label it replaced, and worse than what a person drawing the same header by hand
    would do. On a crowded side the label wins; on a side with room, the symbol does.
    """
    sides: dict[tuple[int, int], list[float]] = {}
    for pin in symbol.pins:
        outward = place_direction(rotation, pin.outward_angle)
        key = (round(outward.x), round(outward.y))
        anchor = place_point(Point(0.0, 0.0), rotation, pin.x, pin.y)
        sides.setdefault(key, []).append(anchor.y if abs(key[0]) else anchor.x)

    dense: set[tuple[int, int]] = set()
    for key, positions in sides.items():
        positions.sort()
        pitch = min(
            (b - a for a, b in pairwise(positions)),
            default=float("inf"),
        )
        if pitch < POWER_REACH:
            dense.add(key)
    return frozenset(dense)


def component_extent(
    component: ElabComponent,
    symbol: Symbol,
    rotation: float,
    power_nets: frozenset[str],
    stubs: Mapping[str, float] | None = None,
) -> Extent:
    """How much room this component needs where it is about to be placed.

    Depends on the rotation, because a connector turned on its side reaches sideways
    with the labels that were above it.
    """
    body = _body_extent(symbol)
    corners = [
        place_point(Point(0.0, 0.0), rotation, x, y)
        for x, y in (
            (-body.left, -body.down), (-body.left, body.up),
            (body.right, -body.down), (body.right, body.up),
        )
    ]
    extent = Extent(
        left=max(0.0, -min(p.x for p in corners)),
        right=max(0.0, max(p.x for p in corners)),
        up=max(0.0, -min(p.y for p in corners)),
        down=max(0.0, max(p.y for p in corners)),
    )

    dense = dense_sides(symbol, rotation)
    for pin in symbol.pins:
        anchor = place_point(Point(0.0, 0.0), rotation, pin.x, pin.y)
        outward = place_direction(rotation, pin.outward_angle)
        net = component.connections.get(pin.number)
        stub = (stubs or {}).get(pin.number, STUB)
        side = (round(outward.x), round(outward.y))
        drawn_as_power = (
            net is not None and net in power_nets and side not in dense
        )
        reach = stub + _terminal_reach(net, drawn_as_power)
        tip = Point(anchor.x + outward.x * reach, anchor.y + outward.y * reach)
        extent = Extent(
            left=max(extent.left, -tip.x, -anchor.x),
            right=max(extent.right, tip.x, anchor.x),
            up=max(extent.up, -tip.y, -anchor.y),
            down=max(extent.down, tip.y, anchor.y),
        )

    return extent


@dataclass(frozen=True, slots=True)
class TextPlace:
    """Where a component's reference and value are written."""

    reference: Point
    value: Point
    justify: str


def text_placement(
    component: ElabComponent, symbol: Symbol, rotation: float, extent: Extent
) -> tuple[TextPlace, Extent]:
    """Put the reference and value outside everything else the component draws.

    Deriving this from the measured extent rather than from the pin positions is the
    whole point: a card-edge connector with nine ground labels hanging below it, or a
    SATA port with a ground symbol standing over its mounting pin, has drawn things
    far past its own pins, and text placed at the pin line lands on all of them. The
    extent already knows where everything is, so the text goes just past it -- and
    the extent is then widened to include the text, so the *next* component is
    placed clear of that too.
    """
    style, reach_x, _ = text_layout(symbol, rotation)
    width = TEXT_CHAR_W * max(len(component.refdes), len(component.display_value))
    if style == "side":
        x = snap(extent.right + 1.27)
        place = TextPlace(
            reference=Point(x, snap(-1.27)), value=Point(x, snap(1.27)), justify="left"
        )
        grown = Extent(
            left=extent.left,
            right=max(extent.right, x + width),
            up=max(extent.up, 2.54),
            down=max(extent.down, 2.54),
        )
        return place, grown

    above = snap(-(extent.up + 1.27 + TEXT_H / 2))
    below = snap(extent.down + 1.27 + TEXT_H / 2)
    anchor = snap(-reach_x) if style == "anchored" else 0.0
    place = TextPlace(
        reference=Point(anchor, above),
        value=Point(anchor, below),
        justify="left" if style == "anchored" else "",
    )
    left = anchor if style == "anchored" else -width / 2
    grown = Extent(
        left=max(extent.left, -left),
        right=max(extent.right, left + width),
        up=extent.up + 1.27 + TEXT_H,
        down=extent.down + 1.27 + TEXT_H,
    )
    return place, grown


def text_layout(symbol: Symbol, rotation: float) -> tuple[str, float, float]:
    """Which of the three text arrangements this symbol asks for.

    A part with pins only above and below -- a standing capacitor -- has its stubs
    and labels exactly where text above and below would land, so its text goes to the
    side. A small part lying down would have half its name reaching back over
    whatever sits on its left-hand stub, which on a grounded part is a ground symbol,
    so its text is anchored at the body and runs right. Everything else is a big
    enough rectangle to carry its name centred above and below.
    """
    vertical = any(
        abs(place_direction(rotation, pin.outward_angle).y) > 0.5 for pin in symbol.pins
    )
    horizontal = any(
        abs(place_direction(rotation, pin.outward_angle).x) > 0.5 for pin in symbol.pins
    )
    reach_x = max(
        (abs(place_point(Point(0, 0), rotation, p.x, p.y).x) for p in symbol.pins),
        default=2.54,
    )
    reach_y = max(
        (abs(place_point(Point(0, 0), rotation, p.x, p.y).y) for p in symbol.pins),
        default=2.54,
    )
    if vertical and not horizontal:
        return "side", reach_x, reach_y
    if len(symbol.pins) <= 3:
        return "anchored", reach_x, reach_y
    return "centred", reach_x, reach_y


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolPlacement:
    """Where one component's symbol sits, and which way round it is."""

    origin: Point
    rotation: float = 0.0


@dataclass(slots=True)
class Block:
    """One visual cluster: a module instance, or a component and its satellites."""

    key: str
    """Stable identity. A module instance name, or the anchor's reference designator."""
    label: str
    """What to write on the cluster's frame. Empty for a bare component."""
    anchors: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    rank: int = 0
    order: int = 0
    extent: Extent = field(default_factory=Extent)
    origin: Point = field(default_factory=lambda: Point(0.0, 0.0))

    @property
    def is_module(self) -> bool:
        return bool(self.label)


@dataclass(slots=True)
class SheetPlan:
    """The computed sheet: every symbol's position, and the structure behind it."""

    paper: str
    width: float
    height: float
    placements: dict[str, SymbolPlacement] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    hosts: dict[str, str] = field(default_factory=dict)
    """Satellite reference designator to the component it serves."""
    ranks: dict[str, int] = field(default_factory=dict)
    """Block key to its position in the left-to-right flow."""
    power_flags: dict[str, Point] = field(default_factory=dict)
    """Net name to where its ``PWR_FLAG`` pin sits."""
    stubs: dict[tuple[str, str], float] = field(default_factory=dict)
    """``(refdes, pin number)`` to how far that pin's stub runs."""
    texts: dict[str, TextPlace] = field(default_factory=dict)
    """Reference designator to where its reference and value are written, relative
    to the symbol's own origin."""

    def stub(self, refdes: str, pin: str) -> float:
        return self.stubs.get((refdes, pin), STUB)

    def rank_of(self, refdes: str) -> int:
        for block in self.blocks:
            if refdes in block.members:
                return block.rank
        return 0


# ---------------------------------------------------------------------------
# step 1 -- satellites
# ---------------------------------------------------------------------------


def _resolve_for(netlist: Netlist, component: ElabComponent) -> str | None:
    """Turn a ``for:`` reference into a reference designator, scoped like the source.

    Same resolution order as the board placer uses, so the two never disagree about
    what a decoupling cap is decoupling.
    """
    if not component.for_ref:
        return None
    by_path = {c.path_text: c.refdes for c in netlist.components.values()}
    scope = component.hier[:-1]
    scoped = ".".join((*scope, component.for_ref))
    if scoped in by_path:
        return by_path[scoped]
    if component.for_ref in by_path:
        return by_path[component.for_ref]
    if component.for_ref in netlist.components:
        return component.for_ref
    return None


def _biased_host(
    netlist: Netlist, component: ElabComponent, power_nets: frozenset[str]
) -> str | None:
    """The component a pull-up or pull-down is biasing, when the source did not say.

    A pull-up with no ``for:`` still belongs beside the thing it holds high, and the
    netlist knows which that is: the net it shares that is not a rail.
    """
    signal_nets = [
        net for net in sorted(set(component.connections.values()))
        if net not in power_nets
    ]
    candidates: list[str] = []
    for net_name in signal_nets:
        net = netlist.nets.get(net_name)
        if net is None:
            continue
        for node in net.nodes:
            if node.refdes == component.refdes:
                continue
            other = netlist.components.get(node.refdes)
            if other is None:
                continue
            candidates.append(node.refdes)
    if not candidates:
        return None
    ranked = sorted(
        set(candidates),
        key=lambda r: (
            0 if (netlist.components[r].role or "") in CENTRE_ROLES else 1,
            0 if (netlist.components[r].role or "") not in SATELLITE_SLOTS else 1,
            r,
        ),
    )
    return ranked[0]


def _satellites(
    netlist: Netlist, power_nets: frozenset[str]
) -> dict[str, str]:
    """Which components hang off which, resolved to a cycle-free host per satellite."""
    direct: dict[str, str] = {}
    for component in netlist.sorted_components():
        role = component.role or ""
        if role not in SATELLITE_SLOTS:
            continue
        host = _resolve_for(netlist, component)
        if host is None and role in ("pull_up", "pull_down"):
            host = _biased_host(netlist, component, power_nets)
        if host is None or host == component.refdes:
            continue
        direct[component.refdes] = host

    # Follow chains -- a cap `for:` a regulator that is itself `for:` something --
    # but never into a cycle, and never more than a few links deep.
    resolved: dict[str, str] = {}
    for refdes, host in sorted(direct.items()):
        seen = {refdes}
        current = host
        for _ in range(4):
            if current not in direct or current in seen:
                break
            seen.add(current)
            current = direct[current]
        if current != refdes:
            resolved[refdes] = current
    return resolved


# ---------------------------------------------------------------------------
# step 2 -- blocks
# ---------------------------------------------------------------------------


def _blocks(netlist: Netlist, hosts: Mapping[str, str]) -> list[Block]:
    """Group components into the clusters that will be placed as units."""
    module_of = {
        c.refdes: (c.hier[0] if len(c.hier) > 1 else "")
        for c in netlist.sorted_components()
    }

    def key_for(refdes: str) -> str:
        module = module_of.get(refdes, "")
        if module:
            return f"module:{module}"
        host = hosts.get(refdes)
        if host is not None and not module_of.get(host, ""):
            return f"part:{host}"
        if host is not None:
            # The host lives in a module and this satellite does not; follow it in,
            # because a cap drawn away from its IC is the thing M14 exists to stop.
            return f"module:{module_of[host]}"
        return f"part:{refdes}"

    blocks: dict[str, Block] = {}
    for component in netlist.sorted_components():
        key = key_for(component.refdes)
        block = blocks.get(key)
        if block is None:
            label = key[7:] if key.startswith("module:") else ""
            block = Block(key=key, label=label)
            blocks[key] = block
        block.members.append(component.refdes)
        if component.refdes not in hosts:
            block.anchors.append(component.refdes)

    for block in blocks.values():
        if not block.anchors:
            # A block of nothing but satellites whose host went elsewhere: the
            # lowest refdes stands in, so the cluster still has something to hang on.
            block.anchors.append(sorted(block.members)[0])
    return [blocks[k] for k in sorted(blocks)]


# ---------------------------------------------------------------------------
# step 3 -- ranking
# ---------------------------------------------------------------------------


def _block_graph(
    netlist: Netlist, blocks: Sequence[Block], power_nets: frozenset[str]
) -> dict[str, set[str]]:
    """Adjacency over signal nets only."""
    owner = {r: b.key for b in blocks for r in b.members}
    graph: dict[str, set[str]] = {b.key: set() for b in blocks}
    for net in netlist.sorted_nets():
        if net.name in power_nets:
            continue
        touched = sorted({owner[n.refdes] for n in net.nodes if n.refdes in owner})
        for i, a in enumerate(touched):
            for b in touched[i + 1 :]:
                graph[a].add(b)
                graph[b].add(a)
    return graph


def _rank(
    netlist: Netlist,
    blocks: Sequence[Block],
    graph: Mapping[str, set[str]],
    power_nets: frozenset[str],
) -> None:
    """Layer the blocks left to right by distance from the design's inputs."""
    roles = {
        b.key: {netlist.components[r].role or "" for r in b.members} for b in blocks
    }
    connectors = [b for b in blocks if roles[b.key] & EDGE_ROLES]

    def inflow(block: Block) -> tuple[int, int, str]:
        """How much this connector looks like the way power and signal *enter*.

        A board's input is the connector the supply comes in on: an
        ``edge_connector`` if the source named one, otherwise whichever connector
        carries the most rail pins. Seeding the flow from every connector at once --
        which is what "connectors on the left" reads like at first -- puts a SATA
        port in the same column as the PCIe edge it is downstream of, and the sheet
        then says the opposite of what the board does.
        """
        edge = any(
            (netlist.components[r].role or "") == "edge_connector" for r in block.members
        )
        rails = sum(
            1
            for r in block.members
            for net in netlist.components[r].connections.values()
            if net in power_nets
        )
        return (1 if edge else 0, rails, block.key)

    sources: list[str] = []
    if connectors:
        best = max(inflow(b)[:2] for b in connectors)
        sources = sorted(b.key for b in connectors if inflow(b)[:2] == best)
    if not sources:
        # Nothing declares itself an input. Start from the least connected block,
        # which is the closest thing the graph has to an edge of the design.
        sources = [min(graph, key=lambda k: (len(graph[k]), k))] if graph else []

    distance: dict[str, int] = {key: 0 for key in sources}
    queue = deque(sorted(sources))
    while queue:
        key = queue.popleft()
        for neighbour in sorted(graph[key]):
            if neighbour not in distance:
                distance[neighbour] = distance[key] + 1
                queue.append(neighbour)

    reached = max(distance.values(), default=0)
    for block in blocks:
        # A block the signal graph never reaches -- a mounting hole, a bulk cap on
        # nothing but rails -- goes in a column of its own past the end, rather than
        # being dropped into the middle of a flow it takes no part in.
        block.rank = distance.get(block.key, reached + 1)

    # Connectors that are not sources are outputs, and outputs belong at the right
    # edge rather than wherever the breadth-first walk happened to stop.
    live = [b for b in blocks if b.key in distance]
    last = max((b.rank for b in live), default=0)
    for block in live:
        if block.rank > 0 and roles[block.key] & EDGE_ROLES:
            block.rank = last


def _order(blocks: Sequence[Block], graph: Mapping[str, set[str]]) -> None:
    """Order each rank so that connected blocks sit across from one another.

    The barycentre heuristic: put a block at the average height of its neighbours in
    the rank before it, sweep forward, sweep back, repeat. Four sweeps is enough on
    boards this size and the result stops moving well before then; ties break on the
    block key so two runs never disagree.
    """
    by_rank: dict[int, list[Block]] = {}
    for block in sorted(blocks, key=lambda b: (b.rank, b.key)):
        by_rank.setdefault(block.rank, []).append(block)
    for members in by_rank.values():
        for index, block in enumerate(members):
            block.order = index

    ranks = sorted(by_rank)
    position = {b.key: float(b.order) for b in blocks}

    def sweep(sequence: Sequence[int], back: bool) -> None:
        for rank in sequence:
            reference = rank + (1 if back else -1)
            if reference not in by_rank:
                continue
            neighbours = {b.key for b in by_rank[reference]}
            scored: list[tuple[float, str, Block]] = []
            for block in by_rank[rank]:
                linked = sorted(graph[block.key] & neighbours)
                centre = (
                    sum(position[k] for k in linked) / len(linked)
                    if linked
                    else position[block.key]
                )
                scored.append((centre, block.key, block))
            scored.sort()
            for index, (_, _, block) in enumerate(scored):
                position[block.key] = float(index)
            by_rank[rank] = [block for _, _, block in scored]

    for _ in range(2):
        sweep(ranks[1:], back=False)
        sweep(list(reversed(ranks[:-1])), back=True)

    for members in by_rank.values():
        for index, block in enumerate(members):
            block.order = index


# ---------------------------------------------------------------------------
# step 4 -- orientation
# ---------------------------------------------------------------------------


def _edge_rotation(symbol: Symbol, facing: float) -> float:
    """Turn a connector so its pins face into the sheet.

    ``facing`` is +1 to face right, -1 to face left. Chosen by measurement rather
    than by a table of part names: whichever quarter turn sends the most pins the
    way we want is the one used, ties going to the smaller angle.
    """
    if not symbol.pins:
        return 0.0
    best = (float("-inf"), 0.0)
    for rotation in (0.0, 90.0, 180.0, 270.0):
        score = 0.0
        for pin in symbol.pins:
            outward = place_direction(rotation, pin.outward_angle)
            score += outward.x * facing
        if score > best[0] + 1e-9:
            best = (score, rotation)
    return best[1]


def _polarised_rotation(
    component: ElabComponent, symbol: Symbol, power_nets: frozenset[str],
    ground_nets: frozenset[str],
) -> float:
    """Stand a two-pin part up with its rail end at the top.

    This is the convention that makes a row of decoupling caps readable at a glance:
    every one of them has its rail on top and its ground at the bottom, so the eye
    reads the row rather than each capacitor.
    """
    if len(symbol.pins) != 2:
        return 0.0
    rail = [p for p in symbol.pins
            if (n := component.connections.get(p.number)) and n in power_nets
            and n not in ground_nets]
    earth = [p for p in symbol.pins
             if (n := component.connections.get(p.number)) and n in ground_nets]
    if not rail:
        return 0.0
    up = rail[0]
    for rotation in (0.0, 180.0, 90.0, 270.0):
        outward = place_direction(rotation, up.outward_angle)
        if outward.y < -0.5:  # sheet Y grows downward, so negative is up
            if earth:
                low = place_direction(rotation, earth[0].outward_angle)
                if low.y < 0.5:
                    continue
            return rotation
    return 0.0


def _rotations(
    netlist: Netlist,
    blocks: Sequence[Block],
    symbols: Mapping[str, Symbol],
    power_nets: frozenset[str],
    ground_nets: frozenset[str],
) -> dict[str, float]:
    """Choose every component's orientation before anything is measured or placed."""
    last = max((b.rank for b in blocks), default=0)
    rotations: dict[str, float] = {}
    for block in blocks:
        for refdes in block.members:
            component = netlist.components[refdes]
            symbol = symbols.get(component.part.symbol) if component.part else None
            if symbol is None:
                rotations[refdes] = 0.0
                continue
            role = component.role or ""
            if role in EDGE_ROLES:
                facing = 1.0 if block.rank < last else -1.0
                rotations[refdes] = _edge_rotation(symbol, facing)
            else:
                rotations[refdes] = _polarised_rotation(
                    component, symbol, power_nets, ground_nets
                )
    return rotations


# ---------------------------------------------------------------------------
# step 5 -- geometry
# ---------------------------------------------------------------------------

#: Sheet sizes in millimetres, smallest first. The page grows to fit the design.
PAPER_SIZES: tuple[tuple[str, float, float], ...] = (
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
)


def _slot_layout(
    host: str,
    satellites: Sequence[str],
    extents: Mapping[str, Extent],
    netlist: Netlist,
) -> dict[str, Point]:
    """Place a host's satellites around it, by the slot each one's role asks for.

    Grouping inside a slot is by the rail a part serves and then by reference
    designator, so the four capacitors on ``P3V3`` stand together and the two on
    ``P1V8`` stand together -- which is how a datasheet draws them, and how a
    reviewer counts them.
    """
    host_extent = extents[host]
    offsets: dict[str, Point] = {}
    slots: dict[str, list[str]] = {}
    for refdes in satellites:
        role = netlist.components[refdes].role or ""
        slots.setdefault(SATELLITE_SLOTS.get(role, "east"), []).append(refdes)

    def rail_key(refdes: str) -> tuple[str, str]:
        component = netlist.components[refdes]
        rails = sorted(set(component.connections.values()))
        return (rails[0] if rails else "", refdes)

    for slot, members in sorted(slots.items()):
        members = sorted(members, key=rail_key)
        if slot in ("north", "south"):
            widths = [extents[r].width + SATELLITE_PITCH for r in members]
            total = sum(widths)
            x = -total / 2
            for refdes, width in zip(members, widths, strict=True):
                centre = x + width / 2
                if slot == "south":
                    y = host_extent.down + SATELLITE_GAP + extents[refdes].up
                else:
                    y = -(host_extent.up + SATELLITE_GAP + extents[refdes].down)
                offsets[refdes] = Point(snap(centre), snap(y))
                x += width
        else:
            heights = [extents[r].height + SATELLITE_PITCH for r in members]
            total = sum(heights)
            y = -total / 2
            for refdes, height in zip(members, heights, strict=True):
                centre = y + height / 2
                if slot == "east":
                    x = host_extent.right + SATELLITE_GAP + extents[refdes].left
                else:
                    x = -(host_extent.left + SATELLITE_GAP + extents[refdes].right)
                offsets[refdes] = Point(snap(x), snap(centre))
                y += height
    return offsets


def _lay_out_block(
    block: Block,
    netlist: Netlist,
    extents: Mapping[str, Extent],
    hosts: Mapping[str, str],
) -> dict[str, Point]:
    """Positions of a block's members, relative to the block's own origin."""
    satellites: dict[str, list[str]] = {}
    for refdes in block.members:
        host = hosts.get(refdes)
        if host is not None and host in block.members:
            satellites.setdefault(host, []).append(refdes)

    loose = [
        r for r in block.members
        if r not in satellites and hosts.get(r) not in block.members
        and hosts.get(r) is not None
    ]
    anchors = [r for r in block.members if hosts.get(r) is None] + loose
    anchors = sorted(set(anchors), key=_refdes_key)
    if not anchors:
        anchors = sorted(block.members, key=_refdes_key)

    offsets: dict[str, Point] = {}
    y = 0.0
    for anchor in anchors:
        around = _slot_layout(anchor, satellites.get(anchor, []), extents, netlist)
        group = Extent(
            left=extents[anchor].left, right=extents[anchor].right,
            up=extents[anchor].up, down=extents[anchor].down,
        )
        for refdes, offset in around.items():
            group = group.union(extents[refdes], offset.x, offset.y)
        centre = y + group.up
        offsets[anchor] = Point(0.0, snap(centre))
        for refdes, offset in around.items():
            offsets[refdes] = Point(snap(offset.x), snap(centre + offset.y))
        y = centre + group.down + BLOCK_GAP

    left = min((offsets[r].x - extents[r].left for r in offsets), default=0.0)
    top = min((offsets[r].y - extents[r].up for r in offsets), default=0.0)
    right = max((offsets[r].x + extents[r].right for r in offsets), default=0.0)
    bottom = max((offsets[r].y + extents[r].down for r in offsets), default=0.0)
    block.extent = Extent(
        left=max(0.0, -left),
        right=max(0.0, right),
        up=max(0.0, -top),
        down=max(0.0, bottom),
    )
    return offsets


def _refdes_key(refdes: str) -> tuple[str, int, str]:
    i = 0
    while i < len(refdes) and not refdes[i].isdigit():
        i += 1
    prefix, rest = refdes[:i], refdes[i:]
    digits = ""
    j = 0
    while j < len(rest) and rest[j].isdigit():
        digits += rest[j]
        j += 1
    return (prefix, int(digits) if digits else 0, rest[j:])


def _paper_for(width: float, height: float) -> tuple[str, float, float]:
    for name, paper_w, paper_h in PAPER_SIZES:
        if width <= paper_w and height <= paper_h:
            return name, paper_w, paper_h
    return PAPER_SIZES[-1]


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


def plan_sheet(
    netlist: Netlist,
    symbols: Mapping[str, Symbol],
    flag_nets: Sequence[str] = (),
) -> SheetPlan:
    """Work out where everything goes, in one pass, deterministically."""
    power_nets = frozenset(
        n.name for n in netlist.sorted_nets() if n.net_class in POWER_CLASSES
    )
    ground_nets = frozenset(
        n.name for n in netlist.sorted_nets() if n.net_class == "ground"
    )

    hosts = _satellites(netlist, power_nets)
    blocks = _blocks(netlist, hosts)
    graph = _block_graph(netlist, blocks, power_nets)
    _rank(netlist, blocks, graph, power_nets)
    _order(blocks, graph)

    rotations = _rotations(netlist, blocks, symbols, power_nets, ground_nets)
    extents: dict[str, Extent] = {}
    texts: dict[str, TextPlace] = {}
    stubs: dict[tuple[str, str], float] = {}
    for component in netlist.sorted_components():
        symbol = symbols.get(component.part.symbol) if component.part else None
        if symbol is None:
            extents[component.refdes] = Extent(2.54, 2.54, 2.54, 2.54)
            continue
        rotation = rotations[component.refdes]
        lengths = _stub_lengths(component, symbol, rotation)
        for number, length in lengths.items():
            stubs[(component.refdes, number)] = length
        bare = component_extent(component, symbol, rotation, power_nets, lengths)
        place, grown = text_placement(component, symbol, rotation, bare)
        texts[component.refdes] = place
        extents[component.refdes] = grown

    inner = {b.key: _lay_out_block(b, netlist, extents, hosts) for b in blocks}

    # Columns, left to right.
    by_rank: dict[int, list[Block]] = {}
    for block in sorted(blocks, key=lambda b: (b.rank, b.order, b.key)):
        by_rank.setdefault(block.rank, []).append(block)

    placements: dict[str, SymbolPlacement] = {}
    x = 0.0
    for rank in sorted(by_rank):
        column = by_rank[rank]
        width = max(b.extent.width for b in column)
        height = sum(b.extent.height for b in column) + BLOCK_GAP * (len(column) - 1)
        y = -height / 2
        for block in column:
            # Placed by its *extent*, not by its origin. A block whose satellites all
            # hang to one side -- an IC with a row of coupling caps east of it -- has
            # an extent that is nothing like symmetric about its anchor, and centring
            # the anchor in the column pushes that side straight out of the column and
            # into the next one's labels.
            slack = (width - block.extent.width) / 2
            block.origin = Point(
                snap(x + slack + block.extent.left), snap(y + block.extent.up)
            )
            for refdes, offset in inner[block.key].items():
                placements[refdes] = SymbolPlacement(
                    Point(
                        snap(block.origin.x + offset.x),
                        snap(block.origin.y + offset.y),
                    ),
                    rotations[refdes],
                )
            y += block.extent.height + BLOCK_GAP
        x += width + COLUMN_GAP

    # A band across the top for the power flags, which belong to the sheet rather
    # than to any block: they say a rail is fed from outside the schematic.
    flags = list(flag_nets)
    drawing_left = min(
        (placements[r].origin.x - extents[r].left for r in placements), default=0.0
    )
    drawing_top = min(
        (placements[r].origin.y - extents[r].up for r in placements), default=0.0
    )
    drawing_right = max(
        (placements[r].origin.x + extents[r].right for r in placements), default=0.0
    )
    drawing_bottom = max(
        (placements[r].origin.y + extents[r].down for r in placements), default=0.0
    )

    flag_pitch = 22.86
    flag_width = flag_pitch * max(0, len(flags) - 1)
    band = FLAG_BAND if flags else 0.0
    left = drawing_left
    top = drawing_top - band
    right = max(drawing_right, left + flag_width)
    bottom = drawing_bottom

    paper, paper_w, paper_h = _paper_for(
        right - left + 2 * MARGIN, bottom - top + 2 * MARGIN
    )
    # Centre the drawing on the page it was given. A design that fits A4 with room
    # to spare should sit in the middle of it, not hard against the top-left corner
    # where the previous grid layout left everything.
    dx = snap((paper_w - (right - left)) / 2 - left)
    dy = snap((paper_h - (bottom - top)) / 2 - top)
    for refdes, placement in list(placements.items()):
        placements[refdes] = SymbolPlacement(
            Point(snap(placement.origin.x + dx), snap(placement.origin.y + dy)),
            placement.rotation,
        )
    for block in blocks:
        block.origin = Point(snap(block.origin.x + dx), snap(block.origin.y + dy))

    flag_positions: dict[str, Point] = {}
    for index, net in enumerate(flags):
        flag_positions[net] = Point(
            snap(left + dx + index * flag_pitch), snap(top + dy + 2.54)
        )

    return SheetPlan(
        paper=paper,
        width=paper_w,
        height=paper_h,
        placements=placements,
        blocks=blocks,
        hosts=dict(hosts),
        ranks={b.key: b.rank for b in blocks},
        power_flags=flag_positions,
        stubs=stubs,
        texts=texts,
    )

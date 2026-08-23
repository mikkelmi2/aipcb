"""Compiling an elaborated netlist into a ``.kicad_sch``.

Connectivity is names. Every pin gets a short stub, and on the end of that stub sits
either a net label or a power symbol carrying the net's name; KiCad joins everything
that shares a name. That is the netlist-first idiom ADR 0003 chose, and it is why
this file can rearrange a whole sheet without any risk of connecting the wrong two
things: the drawing has no say in what is connected.

What M14 changed is everything *else*. Where the symbols go, which way round they
are, which cluster they belong to and what sits on the end of a power pin are now
decided by :mod:`aipcb.compile.sheet` from the roles, ``for:`` references and module
structure the source already carries. The sheet reads left to right, decoupling caps
stand at the IC they decouple, rails point up and grounds point down.

Two things are still emitted purely to satisfy ERC, and both are honest rather than
cosmetic:

* a ``PWR_FLAG`` on every power and ground net KiCad would consider undriven, which
  is how a schematic declares "this rail is fed from somewhere ERC cannot see";
* a no-connect marker on every pin the design deliberately leaves unconnected,
  which is the difference between "I meant this" and "I forgot".

Everything is emitted in sorted order with UUIDs derived from source paths, so the
same design always compiles to byte-identical output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aipcb.compile.geometry import Point, place_direction, place_point
from aipcb.compile.sheet import (
    PAPER_SIZES,
    SheetPlan,
    SymbolPlacement,
    TextPlace,
    dense_sides,
    plan_sheet,
    snap,
)
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.kicad.symbols import Symbol, SymbolNotFound, flatten_symbol, resolve_symbol
from aipcb.kicad.symbols import _load_library as load_symbol_library
from aipcb.netlist import ElabComponent, Netlist

__all__ = [
    "PAPER_SIZES",
    "SCHEMATIC_VERSION",
    "SheetPlan",
    "build_schematic",
    "plan_sheet",
    "undriven_power_nets",
]

#: The ``.kicad_sch`` format version this writer emits. KiCad 9.0 reads and writes it.
SCHEMATIC_VERSION = "20250114"
GENERATOR = "aipcb"
GENERATOR_VERSION = "9.0"

LABEL_FONT = 1.27
PROPERTY_FONT = 1.27
BLOCK_FONT = 1.778

#: The power symbol used to tell ERC a rail is driven.
PWR_FLAG_LIB_ID = "power:PWR_FLAG"

#: Graphics used for a rail and for a ground when the net's own name is not itself
#: a stock power symbol. Both carry the net name in their ``Value``, which is what
#: KiCad names the net after -- these are shapes, not identities.
RAIL_LIB_ID = "power:VCC"
GROUND_LIB_ID = "power:GND"

_POWER_CLASSES = frozenset({"power", "ground"})

#: How far a ``PWR_FLAG`` sits from the power symbol it drives.
FLAG_DROP = 5.08


@dataclass(frozen=True, slots=True)
class _PowerPoint:
    """One place a power symbol has to be drawn."""

    net: str
    at: Point
    lib_id: str
    uuid: str
    rotation: float = 0.0


def undriven_power_nets(netlist: Netlist, symbols: Mapping[str, Symbol]) -> list[str]:
    """Power and ground nets that KiCad would consider undriven.

    A ``PWR_FLAG`` belongs only on a rail with no power-output pin on it. Putting
    one on a rail that already has a real driver -- a regulator output, a connector
    feeding the board -- is itself an ERC error, so this decision is made from the
    *symbols'* pin types rather than the part database's. Those can differ: a part
    may honestly describe a generic connector pin as sourcing power while the stock
    KiCad symbol calls every pin passive, and it is KiCad's view that ERC checks.
    """
    undriven: list[str] = []
    for net in netlist.sorted_nets():
        if net.net_class not in _POWER_CLASSES:
            continue
        driven = False
        for node in net.nodes:
            component = netlist.components.get(node.refdes)
            if component is None or component.part is None:
                continue
            symbol = symbols.get(component.part.symbol)
            pin = symbol.pin(node.pin) if symbol else None
            if pin is not None and pin.type in ("power_out", "output"):
                driven = True
                break
        if not driven:
            undriven.append(net.name)
    return undriven


def power_symbol_for(net_name: str, ground: bool) -> str:
    """Which stock graphic draws this rail.

    A net whose name *is* a stock power symbol gets that symbol, so a design that
    calls its ground ``GND`` or its rail ``+3V3`` comes out drawn exactly the way
    every other KiCad schematic draws it. Anything else gets the generic rail or
    ground shape with its own name written on it, which is what a human does by hand
    when the library has no symbol for ``P3V3``.
    """
    try:
        resolve_symbol(f"power:{net_name}")
    except SymbolNotFound:
        return GROUND_LIB_ID if ground else RAIL_LIB_ID
    return f"power:{net_name}"


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------


def build_schematic(netlist: Netlist, *, project: str | None = None) -> SNode:
    """Compile a netlist into a ``.kicad_sch`` tree.

    Raises :class:`~aipcb.kicad.symbols.SymbolNotFound` if a part's symbol cannot be
    resolved; callers validate first, so reaching that here is a bug rather than
    user error.
    """
    project = project or netlist.name
    # Symbols must be resolved before the plan, because whether a rail needs a
    # PWR_FLAG -- and therefore what has to fit on the sheet -- depends on the
    # symbols' pin types, and how big a cluster is depends on their geometry.
    placed_symbols = _resolve_symbols(netlist, extra=())
    flag_nets = undriven_power_nets(netlist, placed_symbols)
    plan = plan_sheet(netlist, placed_symbols, flag_nets)
    sheet_uuid = element_uuid("sheet", "/")

    ground_nets = frozenset(
        n.name for n in netlist.sorted_nets() if n.net_class == "ground"
    )
    power_nets = frozenset(
        n.name for n in netlist.sorted_nets() if n.net_class in _POWER_CLASSES
    )

    wires, points, markers = _connections(
        netlist, placed_symbols, plan, power_nets, ground_nets
    )
    flag_points, flag_wires = _flag_geometry(plan, flag_nets, ground_nets)
    points.extend(flag_points)
    wires.extend(flag_wires)

    extra = {p.lib_id for p in points}
    if flag_nets:
        extra.add(PWR_FLAG_LIB_ID)
    symbols = _resolve_symbols(netlist, extra=tuple(sorted(extra)))

    root = SNode("kicad_sch")
    root.add(SNode("version").add(sym(SCHEMATIC_VERSION)))
    root.add(SNode("generator").add(quoted(GENERATOR)))
    root.add(SNode("generator_version").add(quoted(GENERATOR_VERSION)))
    root.add(SNode("uuid").add(quoted(sheet_uuid)))
    root.add(SNode("paper").add(quoted(plan.paper)))
    root.add(_title_block(netlist))
    root.add(_lib_symbols(symbols))

    for node in _block_frames(plan):
        root.add(node)

    for component in netlist.sorted_components():
        symbol = symbols[component.part.symbol] if component.part else None
        if symbol is None:
            continue
        root.add(
            _symbol_instance(
                component,
                symbol,
                plan.placements[component.refdes],
                project,
                sheet_uuid,
                plan.texts[component.refdes],
            )
        )

    for index, point in enumerate(points, start=1):
        root.add(
            _power_symbol(point, symbols[point.lib_id], project, sheet_uuid, index)
        )

    for net_name in flag_nets:
        root.add(
            _power_flag(
                net_name,
                plan.power_flags[net_name],
                symbols[PWR_FLAG_LIB_ID],
                project,
                sheet_uuid,
                flag_nets,
                ground=net_name in ground_nets,
            )
        )

    for node in wires:
        root.add(node)
    for node in markers:
        root.add(node)

    root.add(
        SNode("sheet_instances").add(
            SNode("path").add(quoted("/"), SNode("page").add(quoted("1")))
        )
    )
    root.add(SNode("embedded_fonts").add(sym("no")))
    return root


def _title_block(netlist: Netlist) -> SNode:
    """A title block with no date in it.

    KiCad puts today's date here. We do not: a timestamp would make every rebuild
    a diff, which defeats the point of deterministic output.
    """
    block = SNode("title_block")
    block.add(SNode("title").add(quoted(netlist.name)))
    block.add(SNode("rev").add(quoted(netlist.revision)))
    block.add(SNode("company").add(quoted("")))
    if netlist.description:
        block.add(SNode("comment").add(sym("1"), quoted(_one_line(netlist.description))))
    return block


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _resolve_symbols(netlist: Netlist, *, extra: Sequence[str]) -> dict[str, Symbol]:
    wanted = {
        c.part.symbol for c in netlist.components.values() if c.part is not None
    }
    wanted.update(extra)
    return {lib_id: resolve_symbol(lib_id) for lib_id in sorted(wanted)}


def _lib_symbols(symbols: dict[str, Symbol]) -> SNode:
    """Embed a self-contained copy of every symbol the sheet uses."""
    node = SNode("lib_symbols")
    for lib_id in sorted(symbols):
        symbol = symbols[lib_id]
        library = None
        try:
            library = load_symbol_library(symbol.library)
        except SymbolNotFound:  # pragma: no cover - resolve_symbol already succeeded
            library = None
        node.add(flatten_symbol(symbol, library))
    return node


def _effects(
    size: float = PROPERTY_FONT, *, hide: bool = False, justify: str = ""
) -> SNode:
    effects = SNode("effects").add(
        SNode("font").add(SNode("size").add(num(size), num(size)))
    )
    if justify:
        effects.add(SNode("justify").add(*[sym(word) for word in justify.split()]))
    if hide:
        effects.add(SNode("hide").add(sym("yes")))
    return effects


def _property(
    key: str, value: str, at: Point, *, hide: bool = False, justify: str = "",
    angle: float = 0.0,
) -> SNode:
    return SNode("property").add(
        quoted(key),
        quoted(value),
        SNode("at").add(num(at.x), num(at.y), num(angle)),
        _effects(hide=hide, justify=justify),
    )


def _library_flag(symbol: Symbol, token: str) -> str:
    """A symbol's own ``in_bom``/``on_board`` flag, defaulting to yes."""
    node = symbol.node.child(token)
    value = node.value(0) if node is not None else None
    return "no" if value == "no" else "yes"


def _symbol_instance(
    component: ElabComponent,
    symbol: Symbol,
    placement: SymbolPlacement,
    project: str,
    sheet_uuid: str,
    text: TextPlace,
) -> SNode:
    origin = placement.origin
    part = component.part
    assert part is not None  # guaranteed by the caller

    node = SNode("symbol")
    node.add(SNode("lib_id").add(quoted(symbol.lib_id)))
    node.add(SNode("at").add(num(origin.x), num(origin.y), num(placement.rotation)))
    node.add(SNode("unit").add(sym("1")))
    node.add(SNode("exclude_from_sim").add(sym("no")))
    # Follow the library's own answer rather than assuming yes. A mounting hole is
    # declared `in_bom no` by its symbol and `exclude_from_bom` by its footprint,
    # and a schematic that says otherwise makes KiCad's schematic-parity check
    # report every one of them as a mismatch.
    node.add(SNode("in_bom").add(sym(_library_flag(symbol, "in_bom"))))
    node.add(SNode("on_board").add(sym(_library_flag(symbol, "on_board"))))
    node.add(SNode("dnp").add(sym("yes" if component.dnp else "no")))
    node.add(SNode("uuid").add(quoted(component.uuid)))

    # The planner already decided where these go, from the same extent it used to
    # keep the neighbours clear of them.
    reference_at = Point(
        snap(origin.x + text.reference.x), snap(origin.y + text.reference.y)
    )
    value_at = Point(snap(origin.x + text.value.x), snap(origin.y + text.value.y))
    justify = text.justify
    node.add(_property("Reference", component.refdes, reference_at, justify=justify))
    node.add(
        _property("Value", component.display_value, value_at, justify=justify)
    )
    node.add(_property("Footprint", part.footprint, origin, hide=True))
    node.add(_property("Datasheet", part.supplier.datasheet or "", origin, hide=True))
    node.add(_property("Description", part.description or "", origin, hide=True))
    if component.reason:
        # Intent survives into the KiCad file, so a human opening the schematic sees
        # the same rationale the source carries.
        node.add(
            _property("aipcb.reason", _one_line(component.reason), origin, hide=True)
        )
    if component.role:
        node.add(_property("aipcb.role", component.role, origin, hide=True))
    node.add(_property("aipcb.path", component.path_text, origin, hide=True))

    for pin in symbol.pins:
        node.add(
            SNode("pin").add(
                quoted(pin.number),
                SNode("uuid").add(
                    quoted(element_uuid("pin", *component.hier, pin.number))
                ),
            )
        )

    node.add(
        SNode("instances").add(
            SNode("project").add(
                quoted(project),
                SNode("path").add(
                    quoted(f"/{sheet_uuid}"),
                    SNode("reference").add(quoted(component.refdes)),
                    SNode("unit").add(sym("1")),
                ),
            )
        )
    )
    return node


def _power_symbol(
    point: _PowerPoint,
    symbol: Symbol,
    project: str,
    sheet_uuid: str,
    index: int,
) -> SNode:
    """A rail or ground symbol standing on the end of a stub.

    Its ``Value`` is the net name, and a KiCad power symbol names its net after its
    value -- so this carries exactly the connectivity the label it replaced carried,
    in the shape a reader expects to see it in.
    """
    refdes = f"#PWR{index:04d}"
    node = SNode("symbol")
    node.add(SNode("lib_id").add(quoted(point.lib_id)))
    node.add(SNode("at").add(num(point.at.x), num(point.at.y), num(point.rotation)))
    node.add(SNode("unit").add(sym("1")))
    node.add(SNode("exclude_from_sim").add(sym("no")))
    node.add(SNode("in_bom").add(sym("no")))
    node.add(SNode("on_board").add(sym("no")))
    node.add(SNode("dnp").add(sym("no")))
    node.add(SNode("uuid").add(quoted(point.uuid)))
    node.add(_property("Reference", refdes, point.at, hide=True))
    ground = point.lib_id == GROUND_LIB_ID or point.lib_id == "power:GND"
    text_at = Point(point.at.x, snap(point.at.y + (3.81 if ground else -3.81)))
    node.add(_property("Value", point.net, text_at))
    node.add(_property("Footprint", "", point.at, hide=True))
    node.add(_property("Datasheet", "", point.at, hide=True))
    node.add(_property("Description", "", point.at, hide=True))
    for pin in symbol.pins:
        node.add(
            SNode("pin").add(
                quoted(pin.number),
                SNode("uuid").add(quoted(element_uuid("pwrsym-pin", point.uuid, pin.number))),
            )
        )
    node.add(
        SNode("instances").add(
            SNode("project").add(
                quoted(project),
                SNode("path").add(
                    quoted(f"/{sheet_uuid}"),
                    SNode("reference").add(quoted(refdes)),
                    SNode("unit").add(sym("1")),
                ),
            )
        )
    )
    return node


def _power_flag(
    net_name: str,
    position: Point,
    symbol: Symbol,
    project: str,
    sheet_uuid: str,
    order: Sequence[str],
    *,
    ground: bool,
) -> SNode:
    """A ``PWR_FLAG`` declaring that a rail is driven from outside the schematic."""
    uuid = element_uuid("pwrflag", net_name)
    # The flag's own pin points down with its body above. On a ground it therefore
    # stands above the ground symbol as drawn; on a rail it is turned over so the
    # rail symbol keeps the top of the column, which is where a reader looks for it.
    rotation = 0.0 if ground else 180.0
    node = SNode("symbol")
    node.add(SNode("lib_id").add(quoted(PWR_FLAG_LIB_ID)))
    node.add(SNode("at").add(num(position.x), num(position.y), num(rotation)))
    node.add(SNode("unit").add(sym("1")))
    node.add(SNode("exclude_from_sim").add(sym("no")))
    node.add(SNode("in_bom").add(sym("no")))
    node.add(SNode("on_board").add(sym("no")))
    node.add(SNode("dnp").add(sym("no")))
    node.add(SNode("uuid").add(quoted(uuid)))
    refdes = _flag_refdes(net_name, order)
    node.add(_property("Reference", refdes, position, hide=True))
    # The flag body reaches about 2.5 mm past its pin, on the side it is turned to.
    # Its name goes just beyond that, on the same side, where nothing else is.
    text_at = Point(position.x, snap(position.y + (-5.08 if ground else 5.08)))
    node.add(_property("Value", "PWR_FLAG", text_at))
    node.add(_property("Footprint", "", position, hide=True))
    node.add(_property("Datasheet", "", position, hide=True))
    node.add(_property("Description", "", position, hide=True))
    for pin in symbol.pins:
        node.add(
            SNode("pin").add(
                quoted(pin.number),
                SNode("uuid").add(
                    quoted(element_uuid("pwrflag-pin", net_name, pin.number))
                ),
            )
        )
    node.add(
        SNode("instances").add(
            SNode("project").add(
                quoted(project),
                SNode("path").add(
                    quoted(f"/{sheet_uuid}"),
                    SNode("reference").add(quoted(refdes)),
                    SNode("unit").add(sym("1")),
                ),
            )
        )
    )
    return node


def _flag_refdes(net_name: str, order: Sequence[str]) -> str:
    """KiCad's convention: power symbols carry a ``#``-prefixed, numbered refdes.

    The number comes from the net's position in sorted order, so it is stable
    across builds; an unnumbered refdes would make KiCad auto-annotate on load and
    produce a different file than the one we wrote.
    """
    return f"#FLG{order.index(net_name) + 1:02d}"


def _flag_geometry(
    plan: SheetPlan, flag_nets: Sequence[str], ground_nets: frozenset[str]
) -> tuple[list[_PowerPoint], list[SNode]]:
    """A power symbol under each flag, and the wire that ties the two together."""
    points: list[_PowerPoint] = []
    wires: list[SNode] = []
    for net_name in flag_nets:
        flag_at = plan.power_flags[net_name]
        ground = net_name in ground_nets
        # Each symbol's body hangs away from its own pin: a ground's downward, a
        # rail's upward, a flag's whichever way it is turned. So the pair is stacked
        # in the order that leaves both bodies outside the wire between them -- flag
        # above ground, rail above flag -- and neither's text lands on the other.
        symbol_at = Point(
            flag_at.x, snap(flag_at.y + (FLAG_DROP if ground else -FLAG_DROP))
        )
        wires.append(
            _wire(flag_at, symbol_at, element_uuid("pwrflag-wire", net_name))
        )
        points.append(
            _PowerPoint(
                net=net_name,
                at=symbol_at,
                lib_id=power_symbol_for(net_name, ground),
                uuid=element_uuid("pwrflag-sym", net_name),
            )
        )
    return points, wires


def _block_frames(plan: SheetPlan) -> list[SNode]:
    """A light frame and a name around every module instance.

    The module hierarchy is real structure the source declares, and until now it was
    invisible on the sheet. Drawing it is pure graphics -- no pin, no net, nothing
    ERC or the netlister looks at -- and it is what turns "a page of symbols" into
    "three blocks with names".
    """
    nodes: list[SNode] = []
    pad = 3.81
    for block in plan.blocks:
        if not block.is_module:
            continue
        left = snap(block.origin.x - block.extent.left - pad)
        right = snap(block.origin.x + block.extent.right + pad)
        top = snap(block.origin.y - block.extent.up - pad)
        bottom = snap(block.origin.y + block.extent.down + pad)
        nodes.append(
            SNode("rectangle").add(
                SNode("start").add(num(left), num(top)),
                SNode("end").add(num(right), num(bottom)),
                SNode("stroke").add(
                    SNode("width").add(num(0.1524)), SNode("type").add(sym("dash"))
                ),
                SNode("fill").add(SNode("type").add(sym("none"))),
                SNode("uuid").add(quoted(element_uuid("block-frame", block.key))),
            )
        )
        nodes.append(
            SNode("text").add(
                quoted(block.label),
                SNode("exclude_from_sim").add(sym("no")),
                SNode("at").add(num(snap(left + 1.27)), num(snap(top - 1.27)), num(0)),
                _effects(BLOCK_FONT, justify="left bottom"),
                SNode("uuid").add(quoted(element_uuid("block-text", block.key))),
            )
        )
    return nodes


def _connections(
    netlist: Netlist,
    symbols: Mapping[str, Symbol],
    plan: SheetPlan,
    power_nets: frozenset[str],
    ground_nets: frozenset[str],
) -> tuple[list[SNode], list[_PowerPoint], list[SNode]]:
    """A stub for every connected pin, with a label or a power symbol on the end.

    Returns the wires, the power symbols to draw, and the no-connect markers, kept
    apart so the file can be written in a fixed section order.
    """
    wires: list[SNode] = []
    points: list[_PowerPoint] = []
    markers: list[SNode] = []

    for component in netlist.sorted_components():
        if component.part is None:
            continue
        symbol = symbols.get(component.part.symbol)
        if symbol is None:
            continue
        placement = plan.placements[component.refdes]
        dense = dense_sides(symbol, placement.rotation)
        # KiCad's own libraries stack a part's repeated power pins on one coordinate
        # -- the PCIe x1 edge symbol draws its nine grounds as a single pin, and its
        # five 12 V pins as another. Drawing a stub and a label for each of them
        # produces nine identical labels on top of one another, which was 21 of the
        # 22 overlaps left on the pcie-sata sheet. One point carrying one net is one
        # connection, and gets drawn once. Connectivity is unaffected: every pin at
        # that coordinate is joined by the one wire, which is exactly why the library
        # draws them that way.
        drawn: set[tuple[Point, str | None]] = set()

        for pin in symbol.pins:
            anchor, outward = _pin_geometry(symbol, pin.number, placement)
            if anchor is None or outward is None:
                continue
            net_name = component.connections.get(pin.number)
            stacked = (anchor, net_name)
            if stacked in drawn:
                continue
            drawn.add(stacked)
            if net_name is None:
                markers.append(_no_connect(anchor, component, pin.number))
                continue
            stub = plan.stub(component.refdes, pin.number)
            end = Point(
                snap(anchor.x + outward.x * stub), snap(anchor.y + outward.y * stub)
            )
            wires.append(
                _wire(anchor, end, element_uuid("wire", *component.hier, pin.number))
            )
            side = (round(outward.x), round(outward.y))
            if net_name in power_nets and side not in dense:
                points.append(
                    _PowerPoint(
                        net=net_name,
                        at=end,
                        lib_id=power_symbol_for(net_name, net_name in ground_nets),
                        uuid=element_uuid("pwrsym", *component.hier, pin.number),
                    )
                )
            else:
                wires.append(
                    _label(
                        net_name,
                        end,
                        _label_angle(outward),
                        element_uuid("label", *component.hier, pin.number),
                    )
                )
    return wires, points, markers


def _pin_geometry(
    symbol: Symbol, number: str, placement: SymbolPlacement
) -> tuple[Point | None, Point | None]:
    """Return a pin's sheet position and the unit vector pointing away from the body."""
    pin = symbol.pin(number)
    if pin is None:
        return None, None
    anchor = place_point(placement.origin, placement.rotation, pin.x, pin.y).rounded()
    outward = place_direction(placement.rotation, pin.outward_angle).rounded()
    return anchor, outward


def _wire(start: Point, end: Point, uuid: str) -> SNode:
    return SNode("wire").add(
        SNode("pts").add(
            SNode("xy").add(num(start.x), num(start.y)),
            SNode("xy").add(num(end.x), num(end.y)),
        ),
        SNode("stroke").add(
            SNode("width").add(num(0)), SNode("type").add(sym("default"))
        ),
        SNode("uuid").add(quoted(uuid)),
    )


def _label(name: str, at: Point, angle: float, uuid: str) -> SNode:
    """A global label.

    Local labels would work equally well for connectivity, but KiCad names their
    nets after the sheet they sit on -- a label ``VBUS`` on the root sheet becomes
    the net ``/VBUS``. The board would then have to use those prefixed names to
    keep schematic parity, and every net name the toolchain reported would differ
    from the one written in the source. Global labels have no such prefix, and a
    flattened netlist has no sheets to scope anything to anyway.
    """
    justify = "left" if angle in (0.0, 90.0) else "right"
    return SNode("global_label").add(
        quoted(name),
        SNode("shape").add(sym("input")),
        SNode("at").add(num(at.x), num(at.y), num(angle)),
        _effects(LABEL_FONT, justify=justify),
        SNode("uuid").add(quoted(uuid)),
    )


def _label_angle(outward: Point) -> float:
    """Point the label text the way the stub runs."""
    if abs(outward.x) >= abs(outward.y):
        return 0.0 if outward.x >= 0 else 180.0
    return 90.0 if outward.y < 0 else 270.0


def _no_connect(at: Point, component: ElabComponent, pin: str) -> SNode:
    return SNode("no_connect").add(
        SNode("at").add(num(at.x), num(at.y)),
        SNode("uuid").add(quoted(element_uuid("nc", *component.hier, pin))),
    )

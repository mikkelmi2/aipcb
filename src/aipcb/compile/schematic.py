"""Compiling an elaborated netlist into a ``.kicad_sch``.

The schematic this produces is a *correct* schematic, not a pretty one. Symbols are
laid out on a grid, grouped by module instance, and connectivity is expressed with
net labels on short stubs rather than with routed wires between pins. That is the
netlist-first idiom: KiCad joins two pins carrying the same label, so connectivity
is exactly what the source says regardless of where anything sits on the sheet.

Two things are emitted purely to satisfy ERC, and both are honest rather than
cosmetic:

* a ``PWR_FLAG`` on every power and ground net, which is how a KiCad schematic
  declares "this rail is fed from somewhere ERC cannot see";
* a no-connect marker on every pin the design deliberately leaves unconnected,
  which is the difference between "I meant this" and "I forgot".

Everything is emitted in sorted order with UUIDs derived from source paths, so the
same design always compiles to byte-identical output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aipcb.compile.geometry import Point, place_direction, place_point
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.kicad.symbols import Symbol, SymbolNotFound, flatten_symbol, resolve_symbol
from aipcb.kicad.symbols import _load_library as load_symbol_library
from aipcb.netlist import ElabComponent, Netlist

__all__ = ["PAPER_SIZES", "SCHEMATIC_VERSION", "SchematicLayout", "build_schematic"]

#: The ``.kicad_sch`` format version this writer emits. KiCad 9.0 reads and writes it.
SCHEMATIC_VERSION = "20250114"
GENERATOR = "aipcb"
GENERATOR_VERSION = "9.0"

#: Sheet sizes in millimetres, smallest first. The page grows to fit the design.
PAPER_SIZES: tuple[tuple[str, float, float], ...] = (
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
)

#: KiCad connects items by exact coordinate, and its editor works on a 1.27 mm
#: (50 mil) grid. Everything we emit is snapped to it: an off-grid pin is legal but
#: unusable, because a human cannot then draw a wire that meets it.
GRID = 1.27

#: One grid cell per component. Generous, because labels need room around a symbol.
CELL_W = 50.8
CELL_H = 45.72
MARGIN = 25.4
STUB = 3.81
LABEL_FONT = 1.27
PROPERTY_FONT = 1.27

#: The power symbol used to tell ERC a rail is driven.
PWR_FLAG_LIB_ID = "power:PWR_FLAG"
_POWER_CLASSES = frozenset({"power", "ground"})


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one component sits on the sheet."""

    origin: Point
    rotation: float = 0.0


@dataclass(frozen=True, slots=True)
class SchematicLayout:
    """The computed sheet layout, kept separate so it can be inspected and tested."""

    paper: str
    width: float
    height: float
    placements: dict[str, Placement]
    flags: dict[str, Point]
    """Net name to the position of its ``PWR_FLAG``."""


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


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


def plan_layout(netlist: Netlist, flag_nets: Sequence[str] = ()) -> SchematicLayout:
    """Place components on a grid, grouped by module instance.

    Grouping by instance is what makes the sheet navigable at all: everything a
    module contributed lands together, in source order, rather than being scattered
    by reference designator.
    """
    groups = netlist.module_instances()
    ordered: list[ElabComponent] = []
    for key in sorted(groups, key=lambda k: (k != "", k)):
        ordered.extend(groups[key])

    flags = list(flag_nets)
    total = len(ordered) + len(flags)
    columns = max(1, _columns_for(total))

    paper, width, height = _paper_for(total, columns)
    placements: dict[str, Placement] = {}
    for index, component in enumerate(ordered):
        placements[component.refdes] = Placement(_cell_centre(index, columns))

    flag_positions: dict[str, Point] = {}
    for offset, net in enumerate(flags):
        flag_positions[net] = _cell_centre(len(ordered) + offset, columns)

    return SchematicLayout(paper, width, height, placements, flag_positions)


def _cell_centre(index: int, columns: int) -> Point:
    row, col = divmod(index, columns)
    return Point(
        _snap(MARGIN + col * CELL_W + CELL_W / 2),
        _snap(MARGIN + row * CELL_H + CELL_H / 2),
    )


def _snap(value: float, grid: float = GRID) -> float:
    """Round to the connection grid, avoiding floating-point drift in the result."""
    return round(round(value / grid) * grid, 4)


def _columns_for(count: int) -> int:
    """Choose a column count that keeps the sheet roughly as wide as it is tall."""
    columns = 1
    while columns * columns < count:
        columns += 1
    return columns


def _paper_for(count: int, columns: int) -> tuple[str, float, float]:
    rows = -(-count // columns) if columns else 1
    need_w = 2 * MARGIN + columns * CELL_W
    need_h = 2 * MARGIN + rows * CELL_H
    for name, width, height in PAPER_SIZES:
        if need_w <= width and need_h <= height:
            return name, width, height
    return PAPER_SIZES[-1]


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
    # Symbols must be resolved before the layout, because whether a rail needs a
    # PWR_FLAG -- and therefore how many cells the sheet needs -- depends on the
    # symbols' pin types.
    placed_symbols = _resolve_symbols(netlist, needs_power_flag=False)
    flag_nets = undriven_power_nets(netlist, placed_symbols)
    layout = plan_layout(netlist, flag_nets)
    sheet_uuid = element_uuid("sheet", "/")

    root = SNode("kicad_sch")
    root.add(SNode("version").add(sym(SCHEMATIC_VERSION)))
    root.add(SNode("generator").add(quoted(GENERATOR)))
    root.add(SNode("generator_version").add(quoted(GENERATOR_VERSION)))
    root.add(SNode("uuid").add(quoted(sheet_uuid)))
    root.add(SNode("paper").add(quoted(layout.paper)))
    root.add(_title_block(netlist))

    symbols = _resolve_symbols(netlist, needs_power_flag=bool(flag_nets))
    root.add(_lib_symbols(symbols))

    for component in netlist.sorted_components():
        symbol = symbols[component.part.symbol] if component.part else None
        if symbol is None:
            continue
        placement = layout.placements[component.refdes]
        root.add(_symbol_instance(component, symbol, placement, project, sheet_uuid))

    for net_name in flag_nets:
        root.add(
            _power_flag(
                net_name,
                layout.flags[net_name],
                symbols[PWR_FLAG_LIB_ID],
                project,
                sheet_uuid,
                flag_nets,
            )
        )

    for node in _connections(netlist, symbols, layout):
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


def _resolve_symbols(netlist: Netlist, *, needs_power_flag: bool) -> dict[str, Symbol]:
    wanted = {
        c.part.symbol for c in netlist.components.values() if c.part is not None
    }
    if needs_power_flag:
        wanted.add(PWR_FLAG_LIB_ID)
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


def _effects(size: float = PROPERTY_FONT, *, hide: bool = False, justify: str = "") -> SNode:
    effects = SNode("effects").add(SNode("font").add(SNode("size").add(num(size), num(size))))
    if justify:
        effects.add(SNode("justify").add(*[sym(word) for word in justify.split()]))
    if hide:
        effects.add(SNode("hide").add(sym("yes")))
    return effects


def _property(key: str, value: str, at: Point, *, hide: bool = False) -> SNode:
    return SNode("property").add(
        quoted(key),
        quoted(value),
        SNode("at").add(num(at.x), num(at.y), num(0)),
        _effects(hide=hide),
    )


def _symbol_instance(
    component: ElabComponent,
    symbol: Symbol,
    placement: Placement,
    project: str,
    sheet_uuid: str,
) -> SNode:
    origin = placement.origin
    part = component.part
    assert part is not None  # guaranteed by the caller

    node = SNode("symbol")
    node.add(SNode("lib_id").add(quoted(symbol.lib_id)))
    node.add(SNode("at").add(num(origin.x), num(origin.y), num(placement.rotation)))
    node.add(SNode("unit").add(sym("1")))
    node.add(SNode("exclude_from_sim").add(sym("no")))
    node.add(SNode("in_bom").add(sym("yes")))
    node.add(SNode("on_board").add(sym("yes")))
    node.add(SNode("dnp").add(sym("yes" if component.dnp else "no")))
    node.add(SNode("uuid").add(quoted(component.uuid)))

    # Keep the visible text clear of the stub labels, which are rotated to follow
    # their pins and so reach much further than their font size suggests.
    offset = _text_offset(symbol)
    node.add(_property("Reference", component.refdes, Point(origin.x, origin.y - offset)))
    node.add(_property("Value", component.display_value, Point(origin.x, origin.y + offset)))
    node.add(_property("Footprint", part.footprint, origin, hide=True))
    node.add(
        _property("Datasheet", part.supplier.datasheet or "", origin, hide=True)
    )
    node.add(_property("Description", part.description or "", origin, hide=True))
    if component.reason:
        # Intent survives into the KiCad file, so a human opening the schematic sees
        # the same rationale the source carries.
        node.add(_property("aipcb.reason", _one_line(component.reason), origin, hide=True))
    if component.role:
        node.add(_property("aipcb.role", component.role, origin, hide=True))
    node.add(_property("aipcb.path", component.path_text, origin, hide=True))

    for pin in symbol.pins:
        node.add(
            SNode("pin").add(
                quoted(pin.number),
                SNode("uuid").add(quoted(element_uuid("pin", *component.hier, pin.number))),
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


def _text_offset(symbol: Symbol) -> float:
    """How far above and below a symbol its reference and value text should sit."""
    reach = max((abs(pin.y) for pin in symbol.pins), default=0.0)
    return min(_snap(reach + STUB + 10.16), CELL_H / 2 - 2.54)


def _power_flag(
    net_name: str,
    position: Point,
    symbol: Symbol,
    project: str,
    sheet_uuid: str,
    order: Sequence[str],
) -> SNode:
    """A ``PWR_FLAG`` declaring that a rail is driven from outside the schematic."""
    uuid = element_uuid("pwrflag", net_name)
    node = SNode("symbol")
    node.add(SNode("lib_id").add(quoted(PWR_FLAG_LIB_ID)))
    node.add(SNode("at").add(num(position.x), num(position.y), num(0)))
    node.add(SNode("unit").add(sym("1")))
    node.add(SNode("exclude_from_sim").add(sym("no")))
    node.add(SNode("in_bom").add(sym("no")))
    node.add(SNode("on_board").add(sym("no")))
    node.add(SNode("dnp").add(sym("no")))
    node.add(SNode("uuid").add(quoted(uuid)))
    refdes = _flag_refdes(net_name, order)
    node.add(_property("Reference", refdes, position, hide=True))
    node.add(_property("Value", "PWR_FLAG", Point(position.x, position.y - 2.54)))
    node.add(_property("Footprint", "", position, hide=True))
    node.add(_property("Datasheet", "", position, hide=True))
    node.add(_property("Description", "", position, hide=True))
    for pin in symbol.pins:
        node.add(
            SNode("pin").add(
                quoted(pin.number),
                SNode("uuid").add(quoted(element_uuid("pwrflag-pin", net_name, pin.number))),
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


def _connections(
    netlist: Netlist, symbols: dict[str, Symbol], layout: SchematicLayout
) -> list[SNode]:
    """Emit a stub and a label for every connected pin, and markers for the rest."""
    nodes: list[SNode] = []

    for component in netlist.sorted_components():
        if component.part is None:
            continue
        symbol = symbols.get(component.part.symbol)
        if symbol is None:
            continue
        placement = layout.placements[component.refdes]

        for pin in symbol.pins:
            anchor, outward = _pin_geometry(symbol, pin.number, placement)
            if anchor is None or outward is None:
                continue
            net_name = component.connections.get(pin.number)
            if net_name is None:
                nodes.append(_no_connect(anchor, component, pin.number))
                continue
            end = Point(anchor.x + outward.x * STUB, anchor.y + outward.y * STUB)
            nodes.append(_wire(anchor, end, element_uuid("wire", *component.hier, pin.number)))
            nodes.append(
                _label(
                    net_name,
                    end,
                    _label_angle(outward),
                    element_uuid("label", *component.hier, pin.number),
                )
            )

    flag_symbol = symbols.get(PWR_FLAG_LIB_ID)
    if flag_symbol is not None:
        for net_name, position in sorted(layout.flags.items()):
            placement = Placement(position)
            anchor, outward = _pin_geometry(flag_symbol, "1", placement)
            if anchor is None or outward is None:
                continue
            end = Point(anchor.x + outward.x * STUB, anchor.y + outward.y * STUB)
            nodes.append(_wire(anchor, end, element_uuid("pwrflag-wire", net_name)))
            nodes.append(
                _label(
                    net_name,
                    end,
                    _label_angle(outward),
                    element_uuid("pwrflag-label", net_name),
                )
            )
    return nodes


def _pin_geometry(
    symbol: Symbol, number: str, placement: Placement
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
        SNode("stroke").add(SNode("width").add(num(0)), SNode("type").add(sym("default"))),
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

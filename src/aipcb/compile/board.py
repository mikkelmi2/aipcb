"""Compiling an elaborated netlist into a ``.kicad_pcb``.

The board carries the same information as the schematic plus everything layer 2
says about physical construction: the outline, the stackup, the net classes as real
KiCad design rules, and footprints placed according to the design's intent.

Footprint definitions are copied verbatim from KiCad's libraries and then adapted:
the copy is renamed to its full library id, given a position and a UUID, its pads
are attached to nets, and every item inside it gets a deterministic UUID of its
own. Copying rather than modelling means a footprint's graphics, 3D models and
manufacturer metadata all survive intact, including constructs this code knows
nothing about.
"""

from __future__ import annotations

import copy

from aipcb.compile.edge import EDGE_ROLE, strip_edge_graphics
from aipcb.compile.frame import BoardFrame, auto_frame, frame_for
from aipcb.compile.place import BoardPlacement, plan_placement
from aipcb.compile.preserve import FINGERPRINT_PROPERTY, component_fingerprint
from aipcb.compile.zones import apply_pad_connect, pad_zone_connect, zone_nodes
from aipcb.diagnostics import Report
from aipcb.ids import element_uuid, net_codes
from aipcb.kicad.footprints import Extent, footprint_extent, resolve_footprint
from aipcb.kicad.sexpr import Atom, SNode, num, quoted, sym
from aipcb.kicad.symbols import SymbolNotFound, resolve_symbol
from aipcb.model.board import Arc, Segment
from aipcb.model.layout import (
    COPPER_THICKNESS_MM,
    Layout,
    Stackup,
    StackupLayer,
    copper_layer_names,
)
from aipcb.netlist import Netlist

__all__ = [
    "BOARD_VERSION",
    "build_board",
    "edge_geometry",
    "standard_layers",
    "unconnected_net_name",
]

#: The ``.kicad_pcb`` format version this writer emits, as KiCad 9.0 writes it.
BOARD_VERSION = "20241229"
GENERATOR = "aipcb"
GENERATOR_VERSION = "9.0"

EDGE_WIDTH = 0.1
#: KiCad's copper layer numbering: front is 0, back is 2, inner layers are 4, 6, …
FRONT_CU = 0
BACK_CU = 2

_TECHNICAL_LAYERS: tuple[tuple[int, str, str, str], ...] = (
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", ""),
    (15, "B.Paste", "user", ""),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", ""),
    (3, "B.Mask", "user", ""),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", ""),
    (27, "Margin", "user", ""),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", ""),
    (33, "B.Fab", "user", ""),
)


def standard_layers(
    copper_count: int = 2, planes: frozenset[str] = frozenset()
) -> SNode:
    """The layer table for a board with ``copper_count`` copper layers.

    A layer the stackup gave over to a plane is written as ``power`` rather than
    ``signal``, which is how KiCad itself labels one. The router already refuses to
    put signals there; saying so in the board means a human opening it sees the
    same rule.
    """
    node = SNode("layers")

    def role(name: str) -> str:
        return "power" if name in planes else "signal"

    node.add(SNode(str(FRONT_CU)).add(quoted("F.Cu"), sym(role("F.Cu"))))
    for index in range(1, copper_count - 1):
        number = 2 + index * 2
        inner = f"In{index}.Cu"
        node.add(SNode(str(number)).add(quoted(inner), sym(role(inner))))
    node.add(SNode(str(BACK_CU)).add(quoted("B.Cu"), sym(role("B.Cu"))))
    for number, name, kind, alias in _TECHNICAL_LAYERS:
        entry = SNode(str(number)).add(quoted(name), sym(kind))
        if alias:
            entry.add(quoted(alias))
        node.add(entry)
    return node


# ---------------------------------------------------------------------------
# setup and stackup
# ---------------------------------------------------------------------------


def _stackup(layout: Layout | None) -> SNode:
    """The physical stack, which is what impedance and DRC depend on."""
    stack = layout.stackup if layout else None
    copper_count = stack.copper_layers if stack else 2
    finish = (stack.finish if stack else None) or "None"

    node = SNode("stackup")
    node.add(SNode("layer").add(quoted("F.SilkS"), SNode("type").add(quoted("Top Silk Screen"))))
    node.add(SNode("layer").add(quoted("F.Paste"), SNode("type").add(quoted("Top Solder Paste"))))
    node.add(
        SNode("layer").add(
            quoted("F.Mask"),
            SNode("type").add(quoted("Top Solder Mask")),
            SNode("thickness").add(num(0.01)),
        )
    )

    declared = list(stack.layers) if stack and stack.layers else []
    copper_thickness = COPPER_THICKNESS_MM
    # The dielectric takes whatever the copper does not, so the total board
    # thickness matches what the source asked for rather than drifting. The
    # arithmetic lives on `Stackup` because the router needs the same number: a
    # via barrel's length is what it adds to a length-matched net.
    per_dielectric = (stack or Stackup()).dielectric_thickness_mm

    copper_names = list(copper_layer_names(copper_count))
    for index, name in enumerate(copper_names):
        node.add(
            SNode("layer").add(
                quoted(name),
                SNode("type").add(quoted("copper")),
                SNode("thickness").add(num(copper_thickness)),
            )
        )
        if index < len(copper_names) - 1:
            node.add(_dielectric(index + 1, per_dielectric, declared))

    node.add(
        SNode("layer").add(
            quoted("B.Mask"),
            SNode("type").add(quoted("Bottom Solder Mask")),
            SNode("thickness").add(num(0.01)),
        )
    )
    node.add(
        SNode("layer").add(
            quoted("B.Paste"), SNode("type").add(quoted("Bottom Solder Paste"))
        )
    )
    node.add(
        SNode("layer").add(quoted("B.SilkS"), SNode("type").add(quoted("Bottom Silk Screen")))
    )
    node.add(SNode("copper_finish").add(quoted(finish)))
    node.add(SNode("dielectric_constraints").add(sym("no")))
    return node


def _dielectric(index: int, thickness: float, declared: list[StackupLayer]) -> SNode:
    material = "FR4"
    epsilon = 4.5
    for layer in declared:
        if layer.type in ("core", "prepreg"):
            material = layer.material or material
            epsilon = layer.epsilon_r or epsilon
            break
    return SNode("layer").add(
        quoted(f"dielectric {index}"),
        SNode("type").add(quoted("core")),
        SNode("thickness").add(num(thickness)),
        SNode("material").add(quoted(material)),
        SNode("epsilon_r").add(num(epsilon)),
        SNode("loss_tangent").add(num(0.02)),
    )


def _setup(layout: Layout | None, aux_origin: tuple[float, float]) -> SNode:
    node = SNode("setup")
    node.add(_stackup(layout))
    node.add(SNode("pad_to_mask_clearance").add(num(0)))
    node.add(SNode("allow_soldermask_bridges_in_footprints").add(sym("no")))
    node.add(SNode("tenting").add(sym("front"), sym("back")))
    # The drill/place file origin, at the board's bottom-left corner. Export asks
    # kicad-cli for output relative to it (--use-drill-file-origin, --drill-origin
    # plot); without this token that origin silently defaults to the page corner and
    # every drill coordinate comes out negative in Y. See BoardFrame.aux_origin for
    # why this corner and no other.
    node.add(SNode("aux_axis_origin").add(num(aux_origin[0]), num(aux_origin[1])))
    return node


# ---------------------------------------------------------------------------
# outline
# ---------------------------------------------------------------------------


def edge_geometry(frame: BoardFrame) -> list[SNode]:
    """Draw the board edge and every cutout on ``Edge.Cuts``.

    Without an edge KiCad has no board: DRC cannot tell inside from outside and
    fabrication output is meaningless. A cutout is drawn the same way -- KiCad reads
    a closed loop inside the outline as a hole -- so a flex-tail window and the board
    edge itself are one mechanism, which is what lets the router treat them as one
    too.

    The rings arrive already canonicalised by :mod:`aipcb.compile.frame`, so the
    segment order is a function of the board rather than of how the source happened
    to write it.
    """
    nodes: list[SNode] = []
    for index, segment in enumerate(frame.outline):
        nodes.append(_edge_node(segment, element_uuid("edge", index)))
    for cut, ring in enumerate(frame.cutouts):
        for index, segment in enumerate(ring):
            nodes.append(
                _edge_node(segment, element_uuid("edge", "cutout", cut, index))
            )
    return nodes


def _edge_node(segment: Segment, uuid: str) -> SNode:
    """One ``Edge.Cuts`` graphic: a line, or an arc written the way KiCad writes one."""
    stroke = SNode("stroke").add(
        SNode("width").add(num(EDGE_WIDTH)), SNode("type").add(sym("default"))
    )
    if isinstance(segment, Arc):
        return SNode("gr_arc").add(
            SNode("start").add(num(segment.a[0]), num(segment.a[1])),
            SNode("mid").add(num(segment.mid[0]), num(segment.mid[1])),
            SNode("end").add(num(segment.b[0]), num(segment.b[1])),
            stroke,
            SNode("layer").add(quoted("Edge.Cuts")),
            SNode("uuid").add(quoted(uuid)),
        )
    return SNode("gr_line").add(
        SNode("start").add(num(segment.a[0]), num(segment.a[1])),
        SNode("end").add(num(segment.b[0]), num(segment.b[1])),
        stroke,
        SNode("layer").add(quoted("Edge.Cuts")),
        SNode("uuid").add(quoted(uuid)),
    )


def edge_segment_count(frame: BoardFrame) -> tuple[int, tuple[int, ...]]:
    """How many graphics the outline and each cutout produce, for UUID indexing."""
    return len(frame.outline), tuple(len(ring) for ring in frame.cutouts)


def _auto_size(
    placement: BoardPlacement, extents: dict[str, Extent]
) -> tuple[float, float]:
    """A rectangle that contains everything placed, for designs with no outline."""
    if not placement.positions:
        return (50.0, 50.0)
    ox, oy = placement.origin
    max_x = max_y = 0.0
    for placed in placement.positions.values():
        extent = extents.get(placed.refdes)
        span_x = extent.max_x if extent else 1.0
        span_y = extent.max_y if extent else 1.0
        max_x = max(max_x, placed.x - ox + span_x)
        max_y = max(max_y, placed.y - oy + span_y)
    return (round(max_x + 5.0, 2), round(max_y + 5.0, 2))


# ---------------------------------------------------------------------------
# footprints
# ---------------------------------------------------------------------------

#: Tokens a ``.kicad_mod`` file carries that a board's copy must not.
_FILE_ONLY = ("version", "generator", "generator_version")


def _adapt_footprint(
    component_uuid: str,
    lib_id: str,
    source: SNode,
    *,
    refdes: str,
    value: str,
    x: float,
    y: float,
    rotation: float,
    layer: str,
    nets: dict[str, tuple[int, str]],
    hier: tuple[str, ...],
    sheet_file: str,
    dnp: bool,
    fingerprint: str,
    attributes: tuple[str, ...] = (),
    zone_connect: dict[str, int] | None = None,
    drop_edge_graphics: bool = False,
    denied_attributes: frozenset[str] = frozenset(),
) -> SNode:
    """Turn a library footprint into a placed instance on the board."""
    node = copy.deepcopy(source)
    if drop_edge_graphics:
        # A card-edge footprint draws its own `Edge.Cuts`: the tongue, the tip
        # chamfers and the keying notch. Emitting it would give the board outline
        # two authors, and KiCad's own DRC reports the result as a self-intersecting
        # outline the moment the design draws the same edge -- measured, not
        # assumed. The design's `board:` block is the single author, and
        # :mod:`aipcb.checks.edge` is what makes sure it says the same thing.
        strip_edge_graphics(node)
    for token in _FILE_ONLY:
        node.remove(token)
    _set_head(node, lib_id)

    node.remove("layer")
    node.remove("uuid")
    node.remove("at")
    node.remove("path")
    node.remove("sheetname")
    node.remove("sheetfile")

    # The name atom has to stay first; the header goes immediately after it, which
    # is where KiCad writes layer/uuid/at in its own boards.
    insert_at = 0
    while insert_at < len(node.items) and isinstance(node.items[insert_at], Atom):
        insert_at += 1
    node.items[insert_at:insert_at] = [
        SNode("layer").add(quoted(layer)),
        SNode("uuid").add(quoted(component_uuid)),
        SNode("at").add(num(x), num(y), num(rotation)),
    ]

    _set_property(node, "Reference", refdes, "F.SilkS", hier)
    _set_property(node, "Value", value, "F.Fab", hier)
    # Recorded so a later build can tell whether the source has changed its mind
    # about this footprint, and therefore whether a hand-placed position stands.
    _set_property(node, FINGERPRINT_PROPERTY, fingerprint, "F.Fab", hier, hide=True)
    # Every other property keeps the library's text but must not keep the library's
    # UUID: that id is fixed in the .kicad_mod, so all seven 0603 resistors on a
    # board would otherwise share one.
    for prop in node.children("property"):
        key = prop.value(0)
        if key in (None, "Reference", "Value"):
            continue
        prop.remove("uuid")
        prop.add(SNode("uuid").add(quoted(element_uuid("fp-prop", *hier, key))))

    # The path is what ties this footprint to its schematic symbol; without it
    # KiCad's schematic-parity check reports every part as missing.
    node.add(SNode("path").add(quoted(f"/{component_uuid}")))
    node.add(SNode("sheetname").add(quoted("/")))
    node.add(SNode("sheetfile").add(quoted(sheet_file)))
    _set_attributes(node, (*attributes, *(("dnp",) if dnp else ())), denied_attributes)

    _assign_uuids(node, hier)
    _assign_nets(node, nets, hier)
    # Per pad *instance*, and after the nets are on, so the override lands on the
    # same pad the router calls `U2.4#2`.
    apply_pad_connect(node, refdes, zone_connect or {})
    _turn_with_footprint(node, rotation)
    return node


def _set_attributes(
    node: SNode, flags: tuple[str, ...], denied: frozenset[str] = frozenset()
) -> None:
    """Add footprint attribute flags, merging into whatever the library already set.

    KiCad allows one ``attr`` node holding several flags, and it compares those
    flags against the schematic symbol's: a symbol marked "exclude from bill of
    materials" whose footprint is not is reported by ``--schematic-parity``, which
    is exactly what a mounting hole does. Merging rather than appending a second
    ``attr`` matters because ``(attr smd)`` is already there on most footprints --
    and leaving the node alone when there is nothing to add is what keeps every
    board built before this existed byte-identical.
    """
    attr = node.child("attr")
    stale = [
        a for a in (attr.atoms() if attr is not None else ()) if a.value in denied
    ]
    if not flags and not stale:
        return
    if attr is None:
        attr = SNode("attr")
        node.add(attr)
    for atom in stale:
        attr.items.remove(atom)
    present = {a.value for a in attr.atoms()}
    for flag in flags:
        if flag not in present:
            attr.add(sym(flag))
    if not attr.atoms():
        node.remove("attr")


#: What a symbol's library flags mean when written on a footprint instead.
_SYMBOL_ATTRIBUTES = (("in_bom", "exclude_from_bom"), ("on_board", "exclude_from_board"))


def _symbol_attributes(symbol_id: str) -> tuple[str, ...]:
    """The footprint attributes implied by a symbol's own library flags."""
    try:
        symbol = resolve_symbol(symbol_id)
    except SymbolNotFound:  # pragma: no cover - validation catches this earlier
        return ()
    flags: list[str] = []
    for token, attribute in _SYMBOL_ATTRIBUTES:
        node = symbol.node.child(token)
        if node is not None and node.value(0) == "no":
            flags.append(attribute)
    return tuple(flags)


def _contradicted_attributes(symbol_id: str) -> frozenset[str]:
    """Footprint attributes the symbol positively denies.

    KiCad's own libraries do not always agree with themselves. The
    `Connector_PCBEdge:BUS_PCIexpress_x1` footprint is marked
    ``exclude_from_bom`` -- reasonably, since gold fingers are etched rather than
    bought -- while the `Connector:Bus_PCI_Express_x1` symbol says ``in_bom yes``,
    and KiCad's `footprint_symbol_mismatch` DRC rule reports the pair. Somebody has
    to win, and it is the schematic: it is where the netlist and the BOM come from.
    """
    try:
        symbol = resolve_symbol(symbol_id)
    except SymbolNotFound:  # pragma: no cover - validation catches this earlier
        return frozenset()
    denied = {
        attribute
        for token, attribute in _SYMBOL_ATTRIBUTES
        if (node := symbol.node.child(token)) is not None and node.value(0) == "yes"
    }
    return frozenset(denied)


#: Footprint children whose ``at`` angle KiCad stores absolutely, and which therefore
#: have to be turned when the footprint is.
_TURNS_WITH_FOOTPRINT = ("pad", "property", "fp_text")


def _turn_with_footprint(node: SNode, rotation: float) -> None:
    """Carry the footprint's rotation into the things that record their own angle.

    KiCad stores a pad's ``(at x y angle)`` angle *absolutely*, not relative to the
    footprint it sits in: a footprint placed at 90 degrees has every pad written at
    its own angle plus 90. A copy that leaves the library's angles alone therefore
    describes a part whose pads did not turn with it -- KiCad draws the oval pads of
    a rotated header across the board instead of along it, reports the footprint as
    not matching its library, and then reports every track that lands on one of those
    pads as unconnected, because the copper is not where the router thought it was.

    Text works the same way, and gets the same treatment: a reference designator left
    at the library's angle on a turned part lies across the part's own silkscreen.
    """
    if not rotation:
        return
    for item in node.items:
        if not isinstance(item, SNode) or item.name not in _TURNS_WITH_FOOTPRINT:
            continue
        at = item.child("at")
        if at is None:
            continue
        angle = float(at.value(2) or 0) + rotation
        atoms = [i for i, entry in enumerate(at.items) if isinstance(entry, Atom)]
        turned = num(round(angle % 360, 4))
        if len(atoms) >= 3:
            at.items[atoms[2]] = turned
        else:
            at.add(turned)


def _set_head(node: SNode, name: str) -> None:
    for index, item in enumerate(node.items):
        if isinstance(item, Atom):
            node.items[index] = quoted(name)
            return
    node.items.insert(0, quoted(name))


def _set_property(
    node: SNode,
    key: str,
    value: str,
    layer: str,
    hier: tuple[str, ...],
    *,
    hide: bool = False,
) -> None:
    """Set a footprint property, keeping the library's text placement."""
    for prop in node.children("property"):
        if prop.value(0) != key:
            continue
        atoms = [i for i, item in enumerate(prop.items) if isinstance(item, Atom)]
        if len(atoms) >= 2:
            prop.items[atoms[1]] = quoted(value)
        prop.remove("uuid")
        prop.add(SNode("uuid").add(quoted(element_uuid("fp-prop", *hier, key))))
        return

    effects = SNode("effects").add(
        SNode("font").add(
            SNode("size").add(num(1), num(1)), SNode("thickness").add(num(0.15))
        )
    )
    if hide:
        effects.add(SNode("hide").add(sym("yes")))
    node.add(
        SNode("property").add(
            quoted(key),
            quoted(value),
            SNode("at").add(num(0), num(0), num(0)),
            SNode("layer").add(quoted(layer)),
            SNode("uuid").add(quoted(element_uuid("fp-prop", *hier, key))),
            effects,
        )
    )


def _assign_uuids(node: SNode, hier: tuple[str, ...]) -> None:
    """Give each graphic and pad inside a footprint a deterministic UUID.

    Only drawable items carry one. Stamping a uuid onto structural tokens such as
    ``layer`` or ``descr`` produces a file KiCad refuses to open outright, with no
    indication of which token is at fault.

    Deriving the UUIDs from the component's source path and the item's position
    within the footprint keeps them stable across rebuilds, which is what M6's
    edit-preservation matches on.
    """
    counters: dict[str, int] = {}
    for item in node.items:
        if not isinstance(item, SNode) or not _takes_uuid(item.name):
            continue
        index = counters.get(item.name, 0)
        counters[item.name] = index + 1
        key = item.value(0) if item.name == "pad" else str(index)
        item.remove("uuid")
        item.add(
            SNode("uuid").add(
                quoted(element_uuid("fp", *hier, item.name, key or str(index)))
            )
        )


def _takes_uuid(name: str) -> bool:
    """Whether a footprint child is a drawable item, and so carries a UUID."""
    return name.startswith("fp_") or name in ("pad", "zone", "dimension", "group")


def _assign_nets(
    node: SNode, nets: dict[str, tuple[int, str]], hier: tuple[str, ...]
) -> None:
    """Attach each pad to its net. Pads with no net simply carry none."""
    for pad in node.children("pad"):
        number = pad.value(0)
        pad.remove("net")
        if number is None:
            continue
        assignment = nets.get(number)
        if assignment is None:
            continue
        code, name = assignment
        pad.add(SNode("net").add(num(code), quoted(name)))


# ---------------------------------------------------------------------------
# the board
# ---------------------------------------------------------------------------


#: How KiCad escapes a pin name when it builds a net name from one.
_PATH_ESCAPES = {"/": "{slash}", "\\": "{backslash}"}


def unconnected_net_name(refdes: str, pin_name: str, pad: str) -> str:
    """Reproduce the name KiCad gives a pin that connects to nothing.

    KiCad does not leave an unconnected pad netless: it invents a unique net per
    pin so that the pad still belongs somewhere. Leaving those pads bare makes the
    board disagree with the schematic, and ``--schematic-parity`` reports every one
    of them. Matching the convention is what gets parity to zero.

    Only path separators are escaped. Braces are left alone even though they are
    KiCad's own escape delimiters, because a pin name uses them for overbar markup:
    ``XTAL1/PB3`` becomes ``XTAL1{slash}PB3``, and ``~{RESET}/PB5`` becomes
    ``~{RESET}{slash}PB5`` with its braces intact. Both were read off KiCad's own
    parity messages for the ATtiny85, whose pin names happen to cover both cases.
    """
    escaped = "".join(_PATH_ESCAPES.get(ch, ch) for ch in pin_name)
    return f"unconnected-({refdes}-{escaped}-Pad{pad})"


def _unconnected_nets(netlist: Netlist) -> dict[str, dict[str, str]]:
    """Per component, the synthetic net name for each of its unconnected pads."""
    out: dict[str, dict[str, str]] = {}
    for component in netlist.sorted_components():
        if component.part is None:
            continue
        try:
            symbol = resolve_symbol(component.part.symbol)
        except SymbolNotFound:  # pragma: no cover - validation catches this earlier
            continue
        missing: dict[str, str] = {}
        for pin in symbol.pins:
            if pin.number in component.connections:
                continue
            missing[pin.number] = unconnected_net_name(
                component.refdes, pin.name, pin.number
            )
        if missing:
            out[component.refdes] = missing
    return out


def build_board(
    netlist: Netlist, *, project: str | None = None, report: Report | None = None
) -> SNode:
    """Compile a netlist into a ``.kicad_pcb`` tree."""
    project = project or netlist.name
    layout: Layout | None = netlist.layout

    footprints = {
        component.refdes: resolve_footprint(component.part.footprint)
        for component in netlist.sorted_components()
        if component.part is not None
    }
    extents = {refdes: footprint_extent(fp) for refdes, fp in footprints.items()}
    frame = frame_for(netlist)
    placement = plan_placement(netlist, report=report, extents=extents, frame=frame)
    if frame is None:
        frame = auto_frame(*_auto_size(placement, extents), placement.origin)

    zone_connect = pad_zone_connect(netlist)
    unconnected = _unconnected_nets(netlist)
    synthetic = {name for pins in unconnected.values() for name in pins.values()}
    codes = net_codes(set(netlist.nets) | synthetic)
    copper_count = layout.stackup.copper_layers if layout else 2

    root = SNode("kicad_pcb")
    root.add(SNode("version").add(sym(BOARD_VERSION)))
    root.add(SNode("generator").add(quoted(GENERATOR)))
    root.add(SNode("generator_version").add(quoted(GENERATOR_VERSION)))
    root.add(
        SNode("general").add(
            SNode("thickness").add(num(layout.stackup.thickness_mm if layout else 1.6)),
            SNode("legacy_teardrops").add(sym("no")),
        )
    )
    root.add(SNode("paper").add(quoted("A4")))
    root.add(
        SNode("title_block").add(
            SNode("title").add(quoted(netlist.name)),
            SNode("rev").add(quoted(netlist.revision)),
        )
    )
    planes = frozenset(layout.stackup.plane_layers) if layout else frozenset()
    root.add(standard_layers(copper_count, planes))
    root.add(_setup(layout, frame.aux_origin))

    # Net 0 is KiCad's unconnected net and must come first.
    root.add(SNode("net").add(num(0), quoted("")))
    for name in sorted(set(netlist.nets) | synthetic):
        root.add(SNode("net").add(num(codes[name]), quoted(name)))

    sheet_file = f"{project}.kicad_sch"
    for component in netlist.sorted_components():
        if component.part is None:
            continue
        placed = placement.positions.get(component.refdes)
        if placed is None:
            continue
        pad_nets = {
            pin: (codes[net], net)
            for pin, net in component.connections.items()
            if net in codes
        }
        for pin, net in unconnected.get(component.refdes, {}).items():
            pad_nets.setdefault(pin, (codes[net], net))
        root.add(
            _adapt_footprint(
                component.uuid,
                component.part.footprint,
                footprints[component.refdes].node,
                refdes=component.refdes,
                value=component.display_value,
                x=placed.x,
                y=placed.y,
                rotation=placed.rotation,
                layer=placed.layer,
                nets=pad_nets,
                hier=component.hier,
                sheet_file=sheet_file,
                dnp=component.dnp,
                fingerprint=component_fingerprint(component, netlist),
                attributes=_symbol_attributes(component.part.symbol),
                denied_attributes=_contradicted_attributes(component.part.symbol),
                zone_connect=zone_connect,
                drop_edge_graphics=component.role == EDGE_ROLE,
            )
        )

    for segment in edge_geometry(frame):
        root.add(segment)

    # Zones last, and unfilled. The boundary and the rules are source intent; the
    # copper inside them is KiCad's to compute, at check and export time (M10b).
    for zone in zone_nodes(netlist, frame, codes, placement):
        root.add(zone)

    root.add(SNode("embedded_fonts").add(sym("no")))
    return root

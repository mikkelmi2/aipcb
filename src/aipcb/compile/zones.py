"""Emitting copper pours as KiCad zones -- the outline and the rules, never the fill.

A zone in a ``.kicad_pcb`` is two things bolted together: a *boundary* with a set of
fill rules, which is source intent, and a list of ``filled_polygon`` nodes, which is
derived geometry that KiCad's own fill engine produces. This module writes the first
and never the second.

That split is the whole of M10b's stability policy. ``aipcb build`` stays a pure
function of the source and stays byte-stable, because the expensive, geometry-heavy
half is not in it; the fill is regenerated at check and export time from whichever
KiCad is installed (:mod:`aipcb.kicad.fill`). The byte-identical guarantee covers
build output. Fill is a derived artefact -- deterministic in practice, measured in
`ADR 0009 <../decisions/0009-pours.md>`_ Finding 3, but not guaranteed by us,
because it is not ours to guarantee.

The per-pad connection override lives here too. KiCad writes it as a ``zone_connect``
token *inside the pad's own s-expression* -- positional, not referential -- so a
thermal pad can be singled out without ever looking up a UUID. That matters more
than it sounds: pads sharing a number share a UUID today (see ``docs/roadmap.md``),
so a UUID-keyed override would have hit twelve pads instead of one.
"""

from __future__ import annotations

from aipcb.compile.frame import BoardFrame
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.model.board import Ring, tessellate
from aipcb.model.layout import NetClass, copper_layer_names
from aipcb.model.pours import Pour
from aipcb.netlist import Netlist

__all__ = [
    "DEFAULT_MIN_THICKNESS",
    "ZONE_CONNECT",
    "apply_pad_connect",
    "keepout_uuid",
    "keepout_zones",
    "pad_zone_connect",
    "pour_polygon",
    "zone_nodes",
    "zone_uuid",
]

Point = tuple[float, float]

#: KiCad's own default minimum poured width, used when a pour does not say.
DEFAULT_MIN_THICKNESS = 0.25
#: The thermal relief a pour gets when the source does not say.
#:
#: KiCad's own dialog default is 0.5 mm for both, and that is *not* what is used
#: here, because it was measured to produce boards KiCad's own DRC rejects: on a
#: 1.7 mm through-hole pad at 2.54 mm pitch, a 0.5 mm gap leaves only one of the
#: four spokes able to reach the plane, and KiCad 9's `starved_thermal` rule wants
#: at least two. Narrowing the gap to 0.25 mm resolves all four on every bundled
#: example while keeping a real thermal relief -- which is the point of the
#: feature, since a solid connection would make the pad unsolderable by hand.
#: A pour that wants KiCad's figures can still say so.
DEFAULT_THERMAL_GAP = 0.25
DEFAULT_THERMAL_BRIDGE = 0.5
#: The hatch KiCad draws a zone's *outline* with. Cosmetic, and always written.
_OUTLINE_HATCH = 0.5

#: KiCad's ``PAD_ZONE_CONN`` values, as written into a pad's ``zone_connect``.
ZONE_CONNECT = {"none": 0, "thermal": 1, "solid": 2}

#: KiCad's ``island_removal_mode``. ``always`` is mode 0 and KiCad's default, so it
#: is left unwritten -- a board that says nothing must produce the bytes KiCad
#: itself would.
_ISLAND_MODE = {"never": 1, "below_area": 2}


def zone_uuid(index: int) -> str:
    """The stable identity of the zone a pour produces.

    Keyed on the pour's position in ``pours:``, exactly as a cutout is keyed on its
    position in ``cutouts:``. Reordering the block renames the zones, which is the
    same trade the rest of the toolchain already makes for list-shaped source.
    """
    return element_uuid("zone", index)


def keepout_uuid(index: int) -> str:
    """The stable identity of the keepout zone one ``keepouts:`` entry produces."""
    return element_uuid("zone", "keepout", index)


def pour_polygon(pour: Pour, frame: BoardFrame) -> tuple[Point, ...]:
    """The zone boundary in KiCad coordinates.

    A ``scope: board`` pour takes the outline itself: KiCad clips every fill to the
    board edge less the edge clearance, so handing it the true outline asks for
    exactly "all of it" without a second opinion about what the edge is. A region is
    converted through the same single Y-up/Y-down crossing as everything else.
    """
    ring: Ring | None = pour.ring()
    if ring is None:
        return frame.polygon()
    return tuple(frame.to_kicad(point) for point in tessellate(ring))


def zone_nodes(
    netlist: Netlist,
    frame: BoardFrame,
    codes: dict[str, int],
) -> list[SNode]:
    """Every pour, as an unfilled KiCad zone, plus the keepouts they must respect."""
    nodes: list[SNode] = []
    for index, pour in enumerate(netlist.pours):
        code = codes.get(pour.net)
        if code is None:  # pragma: no cover - validation reports the unknown net
            continue
        nodes.append(_zone(pour, index, code, frame, _class_for(netlist, pour.net)))
    if nodes:
        nodes.extend(keepout_zones(netlist))
    return nodes


def keepout_zones(netlist: Netlist) -> list[SNode]:
    """``layout.placement.keepouts``, as KiCad keepout zones that exclude pour.

    The router has always honoured these; the *filler* has not, because it never
    saw them. A pour is the first thing this toolchain emits that would otherwise
    put copper into an area the source said nothing may enter, so the intent has to
    reach KiCad in a form its filler understands -- which is a zone with a
    ``keepout`` block rather than a hole in somebody else's polygon.

    Emitted only when the design declares a pour. Without one there is nothing for
    a keepout zone to keep out that the router does not already handle, and adding
    one would change the bytes of every board built before M10 for no gain.
    """
    layout = netlist.layout
    if layout is None:
        return []
    origin = layout.origin_mm
    copper = copper_layer_names(layout.stackup.copper_layers)
    nodes: list[SNode] = []
    for index, keepout in enumerate(layout.placement.keepouts):
        layers = tuple(keepout.layers) or copper
        node = SNode("zone")
        node.add(SNode("net").add(num(0)))
        node.add(SNode("net_name").add(quoted("")))
        node.add(SNode("layers").add(*(quoted(name) for name in layers)))
        node.add(SNode("uuid").add(quoted(keepout_uuid(index))))
        if keepout.reason:
            node.add(SNode("name").add(quoted(keepout.reason)))
        node.add(SNode("hatch").add(sym("edge"), num(_OUTLINE_HATCH)))
        node.add(SNode("connect_pads").add(SNode("clearance").add(num(0))))
        node.add(SNode("min_thickness").add(num(DEFAULT_MIN_THICKNESS)))
        node.add(SNode("filled_areas_thickness").add(sym("no")))
        node.add(
            SNode("keepout").add(
                SNode("tracks").add(sym(_allowed(not keepout.tracks))),
                SNode("vias").add(sym(_allowed(not keepout.vias))),
                SNode("pads").add(sym(_allowed(not keepout.footprints))),
                # The whole reason this zone exists: the filler must stay out.
                SNode("copperpour").add(sym("not_allowed")),
                SNode("footprints").add(sym(_allowed(not keepout.footprints))),
            )
        )
        node.add(
            SNode("fill").add(
                SNode("thermal_gap").add(num(DEFAULT_THERMAL_GAP)),
                SNode("thermal_bridge_width").add(num(DEFAULT_THERMAL_BRIDGE)),
            )
        )
        x1, y1, x2, y2 = keepout.region_mm
        ox, oy = origin
        corners = (
            (ox + min(x1, x2), oy + min(y1, y2)),
            (ox + max(x1, x2), oy + min(y1, y2)),
            (ox + max(x1, x2), oy + max(y1, y2)),
            (ox + min(x1, x2), oy + max(y1, y2)),
        )
        pts = SNode("pts")
        for x, y in corners:
            pts.add(SNode("xy").add(num(x), num(y)))
        node.add(SNode("polygon").add(pts))
        nodes.append(node)
    return nodes


def _allowed(value: bool) -> str:
    return "allowed" if value else "not_allowed"


def _class_for(netlist: Netlist, net: str) -> NetClass:
    elaborated = netlist.nets.get(net)
    if elaborated is None:
        return NetClass()
    return netlist.net_classes.get(elaborated.net_class, NetClass())


def _zone(
    pour: Pour, index: int, code: int, frame: BoardFrame, net_class: NetClass
) -> SNode:
    node = SNode("zone")
    node.add(SNode("net").add(num(code)))
    node.add(SNode("net_name").add(quoted(pour.net)))

    layers = pour.copper_layers
    if len(layers) == 1:
        node.add(SNode("layer").add(quoted(layers[0])))
    else:
        node.add(SNode("layers").add(*(quoted(name) for name in layers)))

    node.add(SNode("uuid").add(quoted(zone_uuid(index))))
    if pour.name:
        node.add(SNode("name").add(quoted(pour.name)))
    node.add(SNode("hatch").add(sym("edge"), num(_OUTLINE_HATCH)))
    if pour.priority:
        node.add(SNode("priority").add(num(pour.priority)))

    connect = SNode("connect_pads")
    if pour.connect == "solid":
        connect.add(sym("yes"))
    connect.add(SNode("clearance").add(num(pour.clearance or net_class.clearance_mm)))
    node.add(connect)

    node.add(SNode("min_thickness").add(num(pour.min_width or DEFAULT_MIN_THICKNESS)))
    node.add(SNode("filled_areas_thickness").add(sym("no")))
    node.add(_fill(pour))

    pts = SNode("pts")
    for x, y in pour_polygon(pour, frame):
        pts.add(SNode("xy").add(num(x), num(y)))
    node.add(SNode("polygon").add(pts))
    return node


def _fill(pour: Pour) -> SNode:
    """The fill rules. ``yes`` means "this zone is to be filled", not "it is filled"."""
    fill = SNode("fill").add(sym("yes"))
    if pour.hatch is not None:
        fill.add(SNode("mode").add(sym("hatch")))
    fill.add(SNode("thermal_gap").add(num(pour.thermal_gap or DEFAULT_THERMAL_GAP)))
    fill.add(
        SNode("thermal_bridge_width").add(
            num(pour.thermal_bridge_width or DEFAULT_THERMAL_BRIDGE)
        )
    )
    mode = _ISLAND_MODE.get(pour.remove_islands)
    if mode is not None:
        fill.add(SNode("island_removal_mode").add(num(mode)))
    if pour.island_area_min is not None:
        fill.add(SNode("island_area_min").add(num(pour.island_area_min)))
    _hatch_parameters(pour, fill)
    return fill


#: The hatch block is a passthrough: source field to KiCad token, nothing more.
_HATCH_TOKENS: tuple[tuple[str, str], ...] = (
    ("thickness", "hatch_thickness"),
    ("gap", "hatch_gap"),
    ("orientation", "hatch_orientation"),
    ("smoothing_level", "hatch_smoothing_level"),
    ("smoothing_value", "hatch_smoothing_value"),
    ("min_hole_area", "hatch_min_hole_area"),
)


def _hatch_parameters(pour: Pour, fill: SNode) -> None:
    if pour.hatch is None:
        return
    for field_name, token in _HATCH_TOKENS:
        value = getattr(pour.hatch, field_name)
        if value is not None:
            fill.add(SNode(token).add(num(value)))
    if pour.hatch.border_algorithm is not None:
        fill.add(SNode("hatch_border_algorithm").add(sym(pour.hatch.border_algorithm)))


# ---------------------------------------------------------------------------
# per-pad-instance connection overrides
# ---------------------------------------------------------------------------


def pad_zone_connect(netlist: Netlist) -> dict[str, int]:
    """Pad instance to the ``zone_connect`` value the source asks for.

    Keys are pad references as the source writes them: ``U2.4`` means *every* pad
    numbered 4, and ``U2.4#2`` means the second one alone. Both are needed and for
    the same reason -- a receptacle's twelve shield tabs are all pad 6, and
    sometimes they all want flooding and sometimes exactly one does. Later pours
    win, which only matters when two pours disagree about one pad.
    """
    out: dict[str, int] = {}
    for pour in netlist.pours:
        for override in pour.pad_connect:
            for pad in override.pads:
                out[pad] = ZONE_CONNECT[override.connect]
    return out


def apply_pad_connect(footprint: SNode, refdes: str, overrides: dict[str, int]) -> int:
    """Write ``zone_connect`` into the pads the source singled out. Returns how many.

    The pads are walked in file order and keyed by instance, never by UUID: pads
    that share a number share a UUID (``docs/roadmap.md``), so the only thing that
    can address one of twelve identical shield tabs is its position in this list.

    A reference without a ``#`` suffix applies to every pad carrying that number;
    the suffix narrows it to one instance. The instance-specific form is what makes
    the feature work on a footprint where a number is not an identity, and the bare
    form is what makes "flood all four shield tabs" a single line.
    """
    if not overrides:
        return 0
    applied = 0
    seen: dict[str, int] = {}
    for pad in footprint.children("pad"):
        number = pad.value(0)
        if number is None:
            continue
        count = seen.get(number, 0) + 1
        seen[number] = count
        value = overrides.get(f"{refdes}.{number}#{count}")
        if value is None:
            value = overrides.get(f"{refdes}.{number}")
        if value is None:
            continue
        pad.remove("zone_connect")
        pad.add(SNode("zone_connect").add(num(value)))
        applied += 1
    return applied

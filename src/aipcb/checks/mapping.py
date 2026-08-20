"""Mapping KiCad's UUIDs back to the source that produced them.

``kicad-cli``'s ERC and DRC reports identify every offending item by UUID:

```json
{"description": "Symbol J1 [DB9]", "pos": {"x": 0.3, "y": 0.9},
 "uuid": "00000000-0000-0000-0000-0000442a4c93"}
```

Because every UUID we emit is a hash of the element's path in the source, that
field is a perfect reverse index -- no matching on coordinates, no parsing of
description strings, no ambiguity when two parts have the same value. Rebuilding
the same mapping here is just re-deriving the hashes we already know how to
compute.

This is the payoff for the determinism requirement, and the reason ADR 0001 ruled
out a library that rewrites UUIDs.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipcb.ids import element_uuid
from aipcb.kicad.footprints import FootprintNotFound, resolve_footprint
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import ElabComponent, ElabNet, Netlist
from aipcb.source import Loc

__all__ = ["SourceRef", "UuidIndex", "build_index"]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """What a KiCad UUID refers to in the source."""

    kind: str
    """``component``, ``pin``, ``pad``, ``net``, ``label``, ``wire``, ``edge``, …"""
    label: str
    """A human-readable name for it, e.g. ``U1`` or ``U1 pin 8``."""
    path: tuple[str | int, ...] = ()
    loc: Loc | None = None
    component: str | None = None
    net: str | None = None

    def describe(self) -> str:
        return f"{self.kind} {self.label}"


class UuidIndex:
    """Reverse lookup from KiCad UUIDs, and from net names, to source elements."""

    __slots__ = ("_by_net", "_by_refdes", "_by_uuid")

    def __init__(self) -> None:
        self._by_uuid: dict[str, SourceRef] = {}
        self._by_net: dict[str, SourceRef] = {}
        self._by_refdes: dict[str, SourceRef] = {}

    def add(self, uuid: str, ref: SourceRef) -> None:
        self._by_uuid.setdefault(uuid, ref)

    def add_net(self, name: str, ref: SourceRef) -> None:
        self._by_net.setdefault(name, ref)

    def add_refdes(self, refdes: str, ref: SourceRef) -> None:
        self._by_refdes.setdefault(refdes, ref)

    def lookup(self, uuid: str | None) -> SourceRef | None:
        return self._by_uuid.get(uuid) if uuid else None

    def net(self, name: str | None) -> SourceRef | None:
        if not name:
            return None
        # KiCad prefixes sheet-local net names with a path, and invents names for
        # unconnected pads; neither corresponds to a net the source declares.
        return self._by_net.get(name) or self._by_net.get(name.lstrip("/"))

    def refdes(self, refdes: str | None) -> SourceRef | None:
        return self._by_refdes.get(refdes) if refdes else None

    def __len__(self) -> int:
        return len(self._by_uuid)


def build_index(netlist: Netlist) -> UuidIndex:
    """Build the reverse index for a compiled design.

    Every UUID the emitters produce is re-derived here. Keeping the two in step
    matters, so the tests assert that every UUID appearing in a generated file is
    one this index knows.
    """
    index = UuidIndex()

    for component in netlist.sorted_components():
        _index_component(index, component)

    for net in netlist.sorted_nets():
        _index_net(index, net, netlist)

    for position in range(4):
        index.add(
            element_uuid("edge", position),
            SourceRef("board outline", f"edge segment {position + 1}",
                      path=("layout", "outline")),
        )
    return index


def _index_component(index: UuidIndex, component: ElabComponent) -> None:
    ref = SourceRef(
        kind="component",
        label=component.refdes,
        path=component.source_path,
        loc=component.loc,
        component=component.path_text,
    )
    # The same UUID identifies the schematic symbol and the board footprint, which
    # is exactly what ties the two together for schematic parity.
    index.add(component.uuid, ref)
    index.add_refdes(component.refdes, ref)

    pins = component.part.pins if component.part is not None else {}
    for number in pins:
        net = component.connections.get(number)
        pin_ref = SourceRef(
            kind="pin",
            label=f"{component.refdes} pin {number}",
            path=(*component.source_path, "pins", number),
            loc=component.loc,
            component=component.path_text,
            net=net,
        )
        for prefix in ("pin", "wire", "label", "nc"):
            index.add(element_uuid(prefix, *component.hier, number), pin_ref)
        index.add(
            element_uuid("fp", *component.hier, "pad", number),
            SourceRef(
                kind="pad",
                label=f"{component.refdes} pad {number}",
                path=(*component.source_path, "pins", number),
                loc=component.loc,
                component=component.path_text,
                net=net,
            ),
        )

    _index_footprint_items(index, component)


def _index_footprint_items(index: UuidIndex, component: ElabComponent) -> None:
    """Index a footprint's own graphics and text.

    DRC reports against silkscreen and courtyards as readily as against pads --
    ``silk_over_copper`` names an ``fp_line`` -- so those UUIDs have to resolve too,
    or a real violation arrives with nowhere to point. Reading the library footprint
    is the only way to know how many items it has and in what order, which is the
    same order the board writer numbered them in.
    """
    if component.part is None:
        return
    try:
        footprint = resolve_footprint(component.part.footprint)
    except FootprintNotFound:
        return

    body = SourceRef(
        kind="footprint",
        label=component.refdes,
        path=component.source_path,
        loc=component.loc,
        component=component.path_text,
    )

    counters: dict[str, int] = {}
    for item in footprint.node.items:
        if not isinstance(item, SNode):
            continue
        if item.name == "property":
            key = item.value(0)
            if key is not None:
                index.add(
                    element_uuid("fp-prop", *component.hier, key),
                    SourceRef(
                        kind="footprint text",
                        label=f"{component.refdes} {str(key).lower()}",
                        path=component.source_path,
                        loc=component.loc,
                        component=component.path_text,
                    ),
                )
            continue
        if item.name == "pad" or not _takes_uuid(item.name):
            continue
        position = counters.get(item.name, 0)
        counters[item.name] = position + 1
        index.add(element_uuid("fp", *component.hier, item.name, str(position)), body)

    for key in ("Reference", "Value"):
        index.add(
            element_uuid("fp-prop", *component.hier, key),
            SourceRef(
                kind="footprint text",
                label=f"{component.refdes} {key.lower()}",
                path=component.source_path,
                loc=component.loc,
                component=component.path_text,
            ),
        )


def _takes_uuid(name: str) -> bool:
    """Mirrors the board writer's rule for which footprint children carry a UUID."""
    return name.startswith("fp_") or name in ("pad", "zone", "dimension", "group")


def _index_net(index: UuidIndex, net: ElabNet, netlist: Netlist) -> None:
    ref = SourceRef(
        kind="net",
        label=net.name,
        path=net.source_path or ("nets", net.name),
        loc=net.loc,
        net=net.name,
    )
    index.add(net.uuid, ref)
    index.add_net(net.name, ref)
    for prefix in ("pwrflag", "pwrflag-wire", "pwrflag-label"):
        index.add(element_uuid(prefix, net.name), ref)
    index.add(element_uuid("pwrflag-pin", net.name, "1"), ref)

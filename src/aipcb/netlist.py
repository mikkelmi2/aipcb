"""The elaborated netlist -- what a design becomes once modules are expanded.

Layer 1 is hierarchical and parameterised; a schematic is flat. Elaboration is the
step between: it stamps out module instances, resolves each module's local nets
against the ports its parent bound them to, assigns reference designators, and
resolves pin references against the component database.

Everything downstream -- the schematic writer, the board writer, the query layer --
consumes this structure rather than the source model, so there is exactly one
implementation of "what does this design actually contain".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aipcb.ids import element_uuid
from aipcb.model.board import Board
from aipcb.model.design import Constraint, Net
from aipcb.model.layout import Layout, NetClass
from aipcb.model.mech import Fanout, MechPlacement
from aipcb.model.parts import Part
from aipcb.source import Loc

__all__ = ["ElabComponent", "ElabConstraint", "ElabNet", "Netlist", "Node"]


@dataclass(frozen=True, slots=True)
class Node:
    """One pin of one component, attached to a net."""

    refdes: str
    pin: str
    """The pin *number*, as printed in the footprint."""
    pin_name: str
    """The pin's functional name, e.g. ``VCC``."""

    def __str__(self) -> str:
        return f"{self.refdes}.{self.pin}"


@dataclass(frozen=True, slots=True)
class ElabComponent:
    """A component in the flattened design."""

    refdes: str
    part_name: str
    part: Part | None
    """``None`` when the part could not be resolved; diagnostics will say so."""
    hier: tuple[str, ...]
    """Hierarchical path, e.g. ``("supply1", "C1")``. Unique across the design."""
    source_path: tuple[str | int, ...]
    """Where this came from in the source, for diagnostics."""
    loc: Loc | None
    connections: dict[str, str] = field(default_factory=dict)
    """Pin number to net name."""
    role: str | None = None
    for_ref: str | None = None
    reason: str | None = None
    value: str | None = None
    dnp: bool = False

    @property
    def uuid(self) -> str:
        """Stable identity, derived from the hierarchical path -- never from order."""
        return element_uuid("component", *self.hier)

    @property
    def display_value(self) -> str:
        return self.value or self.part_name

    @property
    def path_text(self) -> str:
        return ".".join(self.hier)


@dataclass(frozen=True, slots=True)
class ElabNet:
    """A net in the flattened design, with everything attached to it."""

    name: str
    attrs: Net
    nodes: tuple[Node, ...]
    source_path: tuple[str | int, ...]
    loc: Loc | None
    implicit: bool = False
    """True when the net was never declared under ``nets:``, only used."""

    @property
    def uuid(self) -> str:
        return element_uuid("net", self.name)

    @property
    def net_class(self) -> str:
        return self.attrs.net_class

    @property
    def degree(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True, slots=True)
class ElabConstraint:
    """A constraint with its member names resolved to reference designators."""

    constraint: Constraint
    members: tuple[str, ...]
    source_path: tuple[str | int, ...]
    loc: Loc | None

    @property
    def kind(self) -> str:
        return self.constraint.kind


@dataclass(slots=True)
class Netlist:
    """A fully elaborated design."""

    name: str
    revision: str
    components: dict[str, ElabComponent]
    """Keyed by reference designator."""
    nets: dict[str, ElabNet]
    constraints: tuple[ElabConstraint, ...] = ()
    net_classes: dict[str, NetClass] = field(default_factory=dict)
    layout: Layout | None = None
    board: Board | None = None
    """The mechanical boundary, when the design declares one."""
    placement: dict[str, MechPlacement] = field(default_factory=dict)
    """Mechanical placement, keyed by reference designator."""
    fanout: dict[str, Fanout] = field(default_factory=dict)
    """Fanout intent, keyed by reference designator."""
    unknown_mech_refs: tuple[tuple[str, str], ...] = ()
    """``(block, name)`` for mechanical entries naming no component in the design."""
    mech_names: dict[str, str] = field(default_factory=dict)
    """Reference designator to the name a mechanical block used for it."""
    locs: dict[tuple[str | int, ...], Loc] = field(default_factory=dict)
    """Source positions for the blocks that have no elaborated object of their own."""
    description: str | None = None

    # -- mechanical lookups ----------------------------------------------------

    def anchored(self) -> dict[str, MechPlacement]:
        """Components the source pins, in reference-designator order."""
        return {r: self.placement[r] for r in sorted(self.placement)}

    def fixed_refs(self) -> tuple[str, ...]:
        return tuple(r for r in sorted(self.placement) if self.placement[r].level == "fixed")

    def mech_path(self, block: str, refdes: str, *rest: str | int) -> tuple[str | int, ...]:
        """The source path of a mechanical entry, in the words the source used."""
        return (block, self.mech_names.get(refdes, refdes), *rest)

    def mech_loc(self, block: str, refdes: str) -> Loc | None:
        return self.locs.get((block, self.mech_names.get(refdes, refdes)))

    # -- lookups ---------------------------------------------------------------

    def by_hier(self, path: tuple[str, ...]) -> ElabComponent | None:
        return next((c for c in self.components.values() if c.hier == path), None)

    def by_uuid(self, uuid: str) -> ElabComponent | ElabNet | None:
        """Reverse a KiCad UUID back to the element that owns it.

        This is what makes M4's violation mapping exact rather than positional.
        """
        for component in self.components.values():
            if component.uuid == uuid:
                return component
        for net in self.nets.values():
            if net.uuid == uuid:
                return net
        return None

    def nets_of_class(self, net_class: str) -> list[ElabNet]:
        return [n for n in self.sorted_nets() if n.net_class == net_class]

    def components_with_role(self, role: str) -> list[ElabComponent]:
        return [c for c in self.sorted_components() if c.role == role]

    def module_instances(self) -> dict[str, list[ElabComponent]]:
        """Group components by their top-level instance name."""
        out: dict[str, list[ElabComponent]] = {}
        for component in self.sorted_components():
            key = component.hier[0] if len(component.hier) > 1 else ""
            out.setdefault(key, []).append(component)
        return out

    # -- deterministic ordering ------------------------------------------------

    def sorted_components(self) -> list[ElabComponent]:
        """Components in a stable order: by refdes prefix, then numerically.

        Sorting ``R2`` before ``R10`` matters only for readability, but readability
        of generated files is the point of the whole exercise.
        """
        return sorted(self.components.values(), key=lambda c: _refdes_key(c.refdes))

    def sorted_nets(self) -> list[ElabNet]:
        return sorted(self.nets.values(), key=lambda n: n.name)

    def stats(self) -> dict[str, Any]:
        return {
            "components": len(self.components),
            "nets": len(self.nets),
            "nodes": sum(n.degree for n in self.nets.values()),
            "constraints": len(self.constraints),
        }


def _refdes_key(refdes: str) -> tuple[str, int, str]:
    """Split ``R10`` into ``("R", 10, "")`` so numbers sort numerically."""
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

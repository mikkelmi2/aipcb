"""Partial reads of a design, for token economy.

An agent working on one part of a board should not have to hold the whole thing in
context. These queries answer narrow questions -- what is in this module, what
touches this net, which parts have this role -- and answer them densely: names,
counts and connections, with the prose left out.

Every query returns plain data. The CLI renders it as text or as JSON from the same
structure, so the two can never disagree about what a design contains.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aipcb.netlist import ElabComponent, ElabNet, Netlist

__all__ = [
    "components_by_role",
    "describe_component",
    "describe_module",
    "describe_net",
    "list_modules",
    "nets_of_class",
    "summarise_design",
]


# ---------------------------------------------------------------------------
# whole-design summary
# ---------------------------------------------------------------------------


def summarise_design(netlist: Netlist) -> dict[str, Any]:
    """A one-line-per-block overview: what exists, how big, what it connects to.

    This is meant to be the *first* thing an agent reads about an unfamiliar
    design, and small enough that reading it is never the expensive choice.
    """
    blocks = []
    for name, components in sorted(netlist.module_instances().items()):
        blocks.append(
            {
                "block": name or "(top level)",
                "components": len(components),
                "refdes": _range_summary([c.refdes for c in components]),
                "roles": sorted({c.role for c in components if c.role}),
                "nets": sorted(_nets_touching(components)),
            }
        )

    by_class: dict[str, int] = {}
    for net in netlist.nets.values():
        by_class[net.net_class] = by_class.get(net.net_class, 0) + 1

    return {
        "design": netlist.name,
        "revision": netlist.revision,
        "description": netlist.description,
        "totals": netlist.stats(),
        "net_classes": dict(sorted(by_class.items())),
        "blocks": blocks,
        "constraints": [
            {
                "kind": constraint.kind,
                "members": list(constraint.members),
                "reason": getattr(constraint.constraint, "reason", None),
            }
            for constraint in netlist.constraints
        ],
    }


def _nets_touching(components: Iterable[ElabComponent]) -> set[str]:
    return {net for c in components for net in c.connections.values()}


def _range_summary(refdes: list[str], limit: int = 6) -> str:
    """``R1, R2, R3, … (+4)`` -- enough to recognise, short enough to skim."""
    ordered = sorted(refdes, key=lambda r: (r[:1], len(r), r))
    head = ", ".join(ordered[:limit])
    return head if len(ordered) <= limit else f"{head}, … (+{len(ordered) - limit})"


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------


def list_modules(netlist: Netlist) -> list[str]:
    return sorted(k for k in netlist.module_instances() if k)


def describe_module(netlist: Netlist, instance: str) -> dict[str, Any]:
    """One module instance, plus the parts on the other side of its connections.

    The neighbours are the point. A module read in isolation says nothing about
    what it drives or what drives it, and that context is usually the reason the
    module is being read at all.
    """
    members = [
        component
        for component in netlist.sorted_components()
        if (component.hier[0] if len(component.hier) > 1 else "") == instance
    ]
    if not members:
        raise KeyError(instance)

    inside = {component.refdes for component in members}
    touching = _nets_touching(members)

    boundary: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []
    for name in sorted(touching):
        net = netlist.nets.get(name)
        if net is None:
            continue
        outside = [node for node in net.nodes if node.refdes not in inside]
        entry = {
            "net": name,
            "class": net.net_class,
            "inside": [str(n) for n in net.nodes if n.refdes in inside],
        }
        if outside:
            entry["outside"] = [str(n) for n in outside]
            boundary.append(entry)
        else:
            internal.append(entry)

    neighbours = sorted(
        {node.refdes for entry in boundary for node in netlist.nets[entry["net"]].nodes}
        - inside
    )

    return {
        "module": instance,
        "components": [_component_row(c) for c in members],
        "ports": boundary,
        "internal_nets": internal,
        "neighbours": [
            _component_row(netlist.components[r]) for r in neighbours if r in netlist.components
        ],
    }


def _component_row(component: ElabComponent) -> dict[str, Any]:
    row: dict[str, Any] = {
        "refdes": component.refdes,
        "part": component.part_name,
        "value": component.display_value,
        "path": component.path_text,
    }
    if component.role:
        row["role"] = component.role
    if component.for_ref:
        row["for"] = component.for_ref
    if component.reason:
        row["reason"] = component.reason
    if component.dnp:
        row["dnp"] = True
    return row


# ---------------------------------------------------------------------------
# components and nets
# ---------------------------------------------------------------------------


def describe_component(netlist: Netlist, refdes: str) -> dict[str, Any]:
    """One component: what it is, why, what it connects to, and what is next to it."""
    component = netlist.components.get(refdes)
    if component is None:
        by_path = {c.path_text: c for c in netlist.components.values()}
        component = by_path.get(refdes)
    if component is None:
        raise KeyError(refdes)

    connections = []
    neighbours: set[str] = set()
    for pin, net_name in sorted(component.connections.items()):
        net = netlist.nets.get(net_name)
        others = [str(n) for n in net.nodes if n.refdes != component.refdes] if net else []
        neighbours.update(n.split(".")[0] for n in others)
        pin_name = pin
        if component.part is not None and (definition := component.part.pins.get(pin)):
            pin_name = definition.name or pin
        connections.append(
            {
                "pin": pin,
                "name": pin_name,
                "net": net_name,
                "class": net.net_class if net else None,
                "to": others,
            }
        )

    row = _component_row(component)
    row["connections"] = connections
    row["neighbours"] = sorted(neighbours)
    if component.part is not None:
        row["symbol"] = component.part.symbol
        row["footprint"] = component.part.footprint
    row["served_by"] = [
        c.refdes
        for c in netlist.sorted_components()
        if c.for_ref and c.for_ref in (component.refdes, component.hier[-1])
    ]
    return row


def describe_net(netlist: Netlist, name: str) -> dict[str, Any]:
    net = netlist.nets.get(name)
    if net is None:
        raise KeyError(name)
    return _net_row(net, netlist, full=True)


def _net_row(net: ElabNet, netlist: Netlist, *, full: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "net": net.name,
        "class": net.net_class,
        "nodes": [str(node) for node in net.nodes],
        "degree": net.degree,
    }
    attributes = {
        "voltage": net.attrs.voltage,
        "max_current_a": net.attrs.max_current_a,
        "impedance_ohm": net.attrs.impedance_ohm,
        "diff_pair": net.attrs.diff_pair,
    }
    row.update({k: v for k, v in attributes.items() if v is not None})
    if full:
        if net.attrs.description:
            row["description"] = net.attrs.description
        if net.attrs.reason:
            row["reason"] = net.attrs.reason
        if net.implicit:
            row["implicit"] = True
        rules = netlist.net_classes.get(net.net_class)
        if rules is not None:
            row["rules"] = rules.model_dump(mode="json", exclude_none=True)
        row["parts"] = sorted({node.refdes for node in net.nodes})
    return row


def nets_of_class(netlist: Netlist, net_class: str) -> dict[str, Any]:
    """Every net in a class, with the class's routing rules."""
    nets = netlist.nets_of_class(net_class)
    rules = netlist.net_classes.get(net_class)
    return {
        "class": net_class,
        "rules": rules.model_dump(mode="json", exclude_none=True) if rules else None,
        "count": len(nets),
        "nets": [_net_row(net, netlist) for net in nets],
    }


def components_by_role(netlist: Netlist, role: str) -> dict[str, Any]:
    """Every component with a given role -- ``decoupling``, ``pull_up``, and so on."""
    matches = netlist.components_with_role(role)
    return {
        "role": role,
        "count": len(matches),
        "components": [_component_row(component) for component in matches],
    }

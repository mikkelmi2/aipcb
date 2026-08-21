"""Semantic checks on an elaborated netlist.

These run before anything is compiled, and catch the mistakes that are cheapest to
fix at the source level: a net nothing is connected to, a decoupling capacitor that
does not say what it decouples, a part whose voltage rating is below the rail it
sits on. KiCad's ERC will catch some of the electrical ones later, but only after a
build, and its messages point at coordinates on a sheet rather than at lines in the
file the agent is editing.

Each check is a small function so the set is easy to extend and easy to test.
"""

from __future__ import annotations

from collections.abc import Callable

from aipcb.checks.mech import run_mechanical_checks
from aipcb.diagnostics import Report, summarise
from aipcb.model.parts import ElectricalType
from aipcb.netlist import Netlist

__all__ = ["CHECKS", "run_semantic_checks"]

#: Pin types that are fine to leave unconnected.
_NC_OK = frozenset({ElectricalType.NO_CONNECT, ElectricalType.FREE, ElectricalType.UNSPECIFIED})


def check_dangling_nets(netlist: Netlist, report: Report) -> None:
    """A net with fewer than two nodes connects nothing to anything."""
    for net in netlist.sorted_nets():
        if net.degree >= 2:
            continue
        if net.degree == 1:
            node = net.nodes[0]
            hint = (
                f"only {node} is on this net. If the name is a typo it will not "
                "match the net you meant; if the pin is deliberately unused, give it "
                "a part pin type of no_connect instead"
            )
        else:
            hint = (
                "the net is declared but nothing connects to it; remove it or wire "
                "it up"
            )
        report.error(
            "dangling-net",
            f"net {net.name!r} has {net.degree} connection"
            f"{'s' if net.degree != 1 else ''}",
            loc=net.loc,
            path=net.source_path,
            hint=hint,
            net=net.name,
        )


def check_unconnected_pins(netlist: Netlist, report: Report) -> None:
    """Report pins a part declares that the design never connects."""
    for component in netlist.sorted_components():
        if component.part is None:
            continue
        missing = [
            number
            for number, pin in component.part.pins.items()
            if number not in component.connections and pin.type not in _NC_OK
        ]
        if not missing:
            continue
        report.info(
            "unconnected-pin",
            f"{component.refdes} ({component.part_name}) leaves "
            f"{len(missing)} pin{'s' if len(missing) != 1 else ''} unconnected: "
            f"{summarise(missing)}",
            loc=component.loc,
            path=(*component.source_path, "pins"),
            hint="connect them, or mark them no_connect in the part definition so "
            "ERC does not flag them later",
            component=component.path_text,
        )


def check_for_references(netlist: Netlist, report: Report) -> None:
    """``for:`` must name a component that exists in the same scope."""
    by_hier = {c.path_text: c for c in netlist.components.values()}
    by_refdes = netlist.components

    for component in netlist.sorted_components():
        if not component.for_ref:
            continue
        scope = component.hier[:-1]
        candidates = (
            ".".join((*scope, component.for_ref)),
            component.for_ref,
        )
        if any(c in by_hier for c in candidates) or component.for_ref in by_refdes:
            continue
        siblings = sorted(
            c.hier[-1] for c in netlist.components.values() if c.hier[:-1] == scope
        )
        report.error(
            "unknown-reference",
            f"{component.refdes} says it is `for: {component.for_ref}`, "
            "which is not a component here",
            loc=component.loc,
            path=(*component.source_path, "for"),
            hint=f"components in the same scope: {', '.join(siblings) or 'none'}",
            component=component.path_text,
        )


def check_roles_have_reasons(netlist: Netlist, report: Report) -> None:
    """Intent-carrying roles should say what they serve.

    A capacitor marked ``decoupling`` with no ``for:`` cannot be placement-checked
    later, because nothing says which chip it belongs next to.
    """
    needs_target = {"decoupling", "bypass", "bulk", "pull_up", "pull_down", "snubber"}
    for component in netlist.sorted_components():
        if component.role in needs_target and not component.for_ref:
            report.warning(
                "role-without-target",
                f"{component.refdes} has role {component.role!r} but no `for:`",
                loc=component.loc,
                path=component.source_path,
                hint="add `for: <component>` so placement rules know what it serves",
                component=component.path_text,
            )


def check_diff_pairs(netlist: Netlist, report: Report) -> None:
    """A differential pair must be declared from both sides, and must agree."""
    for net in netlist.sorted_nets():
        partner_name = net.attrs.diff_pair
        if not partner_name:
            continue
        partner = netlist.nets.get(partner_name)
        if partner is None:
            report.error(
                "unknown-diff-pair",
                f"net {net.name!r} names {partner_name!r} as its differential "
                "partner, but there is no such net",
                loc=net.loc,
                path=(*net.source_path, "diff_pair"),
                hint="net names are case-sensitive",
                net=net.name,
            )
            continue
        if partner.attrs.diff_pair != net.name:
            report.error(
                "asymmetric-diff-pair",
                f"net {net.name!r} names {partner_name!r} as its differential "
                f"partner, but {partner_name!r} names "
                f"{partner.attrs.diff_pair or 'nothing'}",
                loc=partner.loc,
                path=(*partner.source_path, "diff_pair"),
                hint=f"set `diff_pair: {net.name}` on {partner_name!r}",
                net=partner.name,
            )
        if net.net_class != partner.net_class:
            report.warning(
                "diff-pair-class-mismatch",
                f"differential partners {net.name!r} and {partner_name!r} are in "
                f"different net classes ({net.net_class!r} and {partner.net_class!r})",
                loc=net.loc,
                path=(*net.source_path, "class"),
                hint="a pair routed to one impedance needs one set of rules",
                net=net.name,
            )


def check_net_class_defined(netlist: Netlist, report: Report) -> None:
    """Warn when a net class carries routing meaning but has no rules."""
    from aipcb.model.design import KNOWN_NET_CLASSES

    used = {n.net_class for n in netlist.nets.values()}
    for name in sorted(used - set(netlist.net_classes)):
        if name in KNOWN_NET_CLASSES:
            continue
        report.warning(
            "undefined-net-class",
            f"net class {name!r} has no rules under `net_classes:`",
            hint="the board will fall back to default track width and clearance",
        )


def check_voltage_ratings(netlist: Netlist, report: Report) -> None:
    """Compare each part's voltage rating against the nets it sits on.

    This is the kind of check the semantic layer exists for: the information is in
    the source (a net's ``voltage:``, a part's ``voltage_max_v``) and nothing
    downstream -- not ERC, not DRC -- will ever look at it.
    """
    for component in netlist.sorted_components():
        part = component.part
        if part is None or part.limits.voltage_max_v is None:
            continue
        rating = part.limits.voltage_max_v
        for pin_number, net_name in sorted(component.connections.items()):
            net = netlist.nets.get(net_name)
            if net is None or net.attrs.voltage is None:
                continue
            voltage = abs(net.attrs.voltage)
            if voltage > rating:
                report.error(
                    "voltage-rating-exceeded",
                    f"{component.refdes} ({component.part_name}) is rated "
                    f"{rating} V but pin {pin_number} sits on net {net_name!r} at "
                    f"{net.attrs.voltage} V",
                    loc=component.loc,
                    path=component.source_path,
                    hint="choose a part with a higher rating, or correct the net's "
                    "`voltage:`",
                    component=component.path_text,
                    net=net_name,
                )
            elif voltage > rating * 0.8:
                report.warning(
                    "voltage-derating",
                    f"{component.refdes} ({component.part_name}) is rated "
                    f"{rating} V and sits on net {net_name!r} at {net.attrs.voltage} V, "
                    f"leaving under 20% margin",
                    loc=component.loc,
                    path=component.source_path,
                    hint="ceramic capacitors in particular lose most of their "
                    "capacitance near their rated voltage",
                    component=component.path_text,
                    net=net_name,
                )


def check_power_nets_driven(netlist: Netlist, report: Report) -> None:
    """A power net should have something that sources it, not only loads."""
    for net in netlist.sorted_nets():
        if net.net_class not in ("power", "ground"):
            continue
        sources = 0
        for node in net.nodes:
            component = netlist.components.get(node.refdes)
            if component is None or component.part is None:
                continue
            pin = component.part.pins.get(node.pin)
            if pin is not None and pin.type in (
                ElectricalType.POWER_OUT,
                ElectricalType.OUTPUT,
            ):
                sources += 1
        if sources == 0:
            report.warning(
                "undriven-power-net",
                f"power net {net.name!r} has no pin that sources it",
                loc=net.loc,
                path=net.source_path,
                hint="a regulator output, a connector pin, or a battery terminal "
                "should have pin type power_out; ERC will flag this too",
                net=net.name,
            )


def check_constraint_members(netlist: Netlist, report: Report) -> None:
    """Constraints must name components that exist."""
    known = {c.path_text for c in netlist.components.values()} | set(netlist.components)
    for constraint in netlist.constraints:
        for member in constraint.members:
            short = member.split(".")[-1]
            if member in known or short in known:
                continue
            report.error(
                "unknown-constraint-member",
                f"{constraint.kind} constraint names {short!r}, which is not a "
                "component in this design",
                loc=constraint.loc,
                path=constraint.source_path,
                hint="constraints refer to components by their source name, "
                "not their reference designator",
            )


#: Every check, run in this order.
CHECKS: tuple[Callable[[Netlist, Report], None], ...] = (
    check_dangling_nets,
    check_for_references,
    check_constraint_members,
    check_diff_pairs,
    check_voltage_ratings,
    check_power_nets_driven,
    check_net_class_defined,
    check_roles_have_reasons,
    check_unconnected_pins,
    # Mechanical conflicts last, because they are the only ones that need geometry
    # and the electrical problems above are the ones worth reading first.
    run_mechanical_checks,
)


def run_semantic_checks(netlist: Netlist, report: Report) -> Report:
    """Run every semantic check against ``netlist``, accumulating diagnostics."""
    for check in CHECKS:
        check(netlist, report)
    return report

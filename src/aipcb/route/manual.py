"""Declared-manual routing, and the four states a net's copper can be in.

Manual routing already *happened* before M14: the router hands a connection over
when it cannot deliver legal geometry, and M6 preserves whatever a human draws in
KiCad. What did not exist was a way to say so *in advance*. "This pair is mine, do
not route it" was a fact about a workflow rather than a fact about the design --
undiffable, unvalidatable, and impossible for an agent to act on without being told
out of band.

``routing: manual`` makes it a declaration. On a net class it covers every net in
the class; on a single net it overrides the class either way. The router never
touches a declared-manual net, and every report distinguishes four states rather
than the two ("routed" / "unrouted") that the field would otherwise collapse into:

======================  ====================================================
``manual-routed``       declared manual, and copper for it is on the board
``manual-pending``      declared manual, and there is no copper yet
``auto-routed``         aipcb's router laid it
``handed-over``         aipcb's router tried and refused; the reason is given
======================  ====================================================

The distinction that matters most is the second one. A net that is *pending* looks
exactly like a routed one to anything that only counts unrouted connections, and it
is the state a board sits in between "declare the critical pairs manual" and
"actually draw them" -- which is precisely when a silent report would let a board go
to fab with a lane missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from aipcb.kicad.sexpr import SNode
from aipcb.netlist import Netlist

__all__ = [
    "MANUAL",
    "STATES",
    "NetRouting",
    "RoutingStates",
    "is_manual",
    "manual_nets",
    "net_routing_mode",
    "nets_with_copper",
    "routing_states",
]

MANUAL = "manual"

#: Every state a net's copper can be in, in the order a report lists them.
STATES = ("manual-routed", "manual-pending", "auto-routed", "handed-over", "unrouted")

Mode = Literal["auto", "manual"]


def net_routing_mode(netlist: Netlist, net_name: str) -> Mode:
    """Whether this net is routed by aipcb or by hand.

    The net's own ``routing:`` wins over its class's, in both directions: a class
    declared manual can still let one net through on the autorouter, which is what
    you want for the ground return of an otherwise hand-drawn group.
    """
    net = netlist.nets.get(net_name)
    if net is not None and net.attrs.routing is not None:
        return "manual" if net.attrs.routing == MANUAL else "auto"
    net_class = netlist.net_classes.get(net.net_class if net else "")
    if net_class is not None and net_class.routing == MANUAL:
        return "manual"
    return "auto"


def is_manual(netlist: Netlist, net_name: str) -> bool:
    return net_routing_mode(netlist, net_name) == "manual"


def manual_nets(netlist: Netlist) -> list[str]:
    """Every declared-manual net, in name order."""
    return [n.name for n in netlist.sorted_nets() if is_manual(netlist, n.name)]


def nets_with_copper(board: SNode, netlist: Netlist) -> set[str]:
    """Which nets have any track, arc or via on the board.

    Read off the board rather than tracked through the pipeline, because the whole
    point of a manual net is that its copper may have arrived by a route this
    program knows nothing about -- a hand route in KiCad, or a Specctra session file
    from an external router.
    """
    names = set(netlist.nets)
    found: set[str] = set()
    for kind in ("segment", "arc", "via"):
        for item in board.children(kind):
            net = item.child("net")
            if net is None:
                continue
            value = net.value(0)
            if value in names:
                found.add(value)
                continue
            # KiCad boards carry net *numbers* on copper and the name table
            # separately; resolve through it when that is the form in use.
            resolved = _net_name(board, value)
            if resolved in names:
                found.add(resolved)
    return found


def _net_name(board: SNode, number: str | None) -> str | None:
    if number is None:
        return None
    for net in board.children("net"):
        if net.value(0) == number:
            return net.value(1)
    return None


@dataclass(frozen=True, slots=True)
class NetRouting:
    """One net's routing state, and what put it there."""

    net: str
    net_class: str
    state: str
    declared: Mode
    reason: str = ""
    controlled_impedance: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "net": self.net,
            "class": self.net_class,
            "state": self.state,
            "declared": self.declared,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.controlled_impedance:
            payload["controlled_impedance"] = True
        return payload


@dataclass(slots=True)
class RoutingStates:
    """Every net on the board, in one of the four states."""

    nets: list[NetRouting]

    def of_state(self, state: str) -> list[NetRouting]:
        return [n for n in self.nets if n.state == state]

    @property
    def pending(self) -> list[NetRouting]:
        """Declared-manual nets that still have no copper. The ones that bite."""
        return self.of_state("manual-pending")

    def counts(self) -> dict[str, int]:
        return {
            state: len(self.of_state(state))
            for state in STATES
            if self.of_state(state)
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "nets": [n.to_dict() for n in self.nets],
            "manual_pending": [n.net for n in self.pending],
        }


def routing_states(
    board: SNode,
    netlist: Netlist,
    *,
    auto_routed: set[str] | None = None,
    handed_over: dict[str, str] | None = None,
) -> RoutingStates:
    """Classify every net that needs copper into one of the four states.

    ``auto_routed`` and ``handed_over`` come from the router when one has just run.
    Without them -- ``aipcb check --no-route`` on a board somebody else routed --
    every net with copper on it that is not declared manual is reported as
    auto-routed, which is the honest reading: the board has copper and this program
    did not just put it there.
    """
    with_copper = nets_with_copper(board, netlist)
    auto = auto_routed or set()
    handed = handed_over or {}

    states: list[NetRouting] = []
    for net in netlist.sorted_nets():
        if net.degree < 2:
            continue  # nothing to connect
        net_class = netlist.net_classes.get(net.net_class)
        declared: Mode = net_routing_mode(netlist, net.name)
        controlled = bool(net_class and net_class.controlled_impedance)
        if declared == "manual":
            state = "manual-routed" if net.name in with_copper else "manual-pending"
            reason = (
                ""
                if net.name in with_copper
                else "declared `routing: manual` and has no copper yet"
            )
        elif net.name in handed:
            state, reason = "handed-over", handed[net.name]
        elif net.name in auto or net.name in with_copper:
            state, reason = "auto-routed", ""
        else:
            state, reason = "unrouted", "no copper, and nothing declared it manual"
        states.append(
            NetRouting(
                net=net.name,
                net_class=net.net_class,
                state=state,
                declared=declared,
                reason=reason,
                controlled_impedance=controlled,
            )
        )
    return RoutingStates(states)

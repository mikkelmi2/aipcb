# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Validating route topologies against a placed board.

A sketch can be well-formed and still impossible: it can name a pad that is not on
its net, ask to pass an obstacle that no longer exists, or describe a path around
one side of a part that placement has since made unreachable. This module answers
"is this sketch realizable *here*", where "here" is the current placement.

Realizability is not decided by inspection. The sketch is turned into a homotopy
class against a triangulation of the actual free space, exactly as the stretcher
would, and a class that cannot be built is one that does not exist.

There is a second question a per-route check cannot answer: whether the sketches fit
*alongside each other*. Every cut across the free space has a capacity, and a set of
routes that over-subscribes one cannot be built however sound each of them is on its
own. That is what :func:`check_capacity` adds, across every layer at once -- as a
lower bound rather than as the criterion in full, which that function's docstring is
careful to say out loud.
"""

from __future__ import annotations

from dataclasses import replace

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.costs import DEFAULT_COSTS
from aipcb.route.field import build_field, keepout_obstacles
from aipcb.route.geometry import edge_clearance_for
from aipcb.route.model import Pass, RouteTopology, ViaHop
from aipcb.route.obstacles import RoutingEnvironment, extract_obstacles
from aipcb.route.plan import rules_for
from aipcb.route.stack import stack_for
from aipcb.route.stretch import (
    LayerGeometry,
    RouteRules,
    StretchError,
    stretch_route,
)
from aipcb.route.triangulate import (
    FreeSpaceError,
    free_space,
    triangulate_free,
)

__all__ = ["RouteCheck", "check_capacity", "check_routes"]


class RouteCheck:
    """The outcome of checking every route topology in a design."""

    __slots__ = ("over_subscribed", "realizable", "unrealizable")

    def __init__(self) -> None:
        self.realizable: list[RouteTopology] = []
        self.unrealizable: list[tuple[RouteTopology, str]] = []
        self.over_subscribed: list[dict[str, object]] = []
        """Cuts that carry more copper than they have room for, across all layers."""

    @property
    def ok(self) -> bool:
        return not self.unrealizable and not self.over_subscribed

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": len(self.realizable) + len(self.unrealizable),
            "realizable": [r.key() for r in self.realizable],
            "unrealizable": [
                {"route": r.key(), "reason": why} for r, why in self.unrealizable
            ],
            "over_subscribed": self.over_subscribed,
        }


def check_routes(board: SNode, netlist: Netlist, report: Report) -> RouteCheck:
    """Check every ``layout.routes`` entry against the placed board."""
    outcome = RouteCheck()
    topologies = tuple(netlist.layout.routes) if netlist.layout else ()
    environment = extract_obstacles(board, edge_clearance=edge_clearance_for(netlist))
    check_capacity(board, netlist, report, outcome, topologies)
    if not topologies:
        return outcome

    net_pads: dict[str, set[str]] = {}
    for name, net in netlist.nets.items():
        net_pads[name] = {f"{node.refdes}.{node.pin}" for node in net.nodes}

    for index, route in enumerate(topologies):
        path: tuple[str | int, ...] = ("layout", "routes", index)
        if _structural_problems(route, netlist, net_pads, environment, report, path):
            outcome.unrealizable.append((route, "the sketch does not match the design"))
            continue

        rules = rules_for(netlist, route.net)
        try:
            stretch_route(
                route,
                environment,
                _layer_geometry(environment, netlist, route, rules),
                rules,
                stack=stack_for(netlist.layout),
            )
        except (StretchError, FreeSpaceError) as exc:
            reason = getattr(exc, "message", str(exc))
            outcome.unrealizable.append((route, reason))
            report.error(
                "route-unrealizable",
                f"route {route.key()} cannot be built as sketched: {reason}",
                path=path,
                hint=getattr(exc, "hint", None)
                or "pass the obstacle on the other side, add a via hop, or move the "
                "parts apart",
                net=route.net,
            )
            continue
        outcome.realizable.append(route)

    return outcome


def check_capacity(
    board: SNode,
    netlist: Netlist,
    report: Report,
    outcome: RouteCheck,
    topologies: tuple[RouteTopology, ...],
) -> None:
    """Decide whether the declared routes fit, cut by cut, across every layer.

    This is Maley's realizability criterion and SURF's routability test, applied to
    the sketches the source wrote: every interior edge of every layer's
    triangulation is a *cut* across the free space, and a set of routes can be
    turned into legal geometry exactly when no cut carries more track-plus-clearance
    than its length allows.

    It is a different question from "can this one route be built", which the rest of
    this module answers by building it. A sketch can be perfectly realizable on its
    own and impossible alongside the three others that want the same corridor, and
    the only way to see that is to add the corridors up.

    **What this does not promise.** Maley's criterion quantifies over every cut
    across the free space. This charges two families of them: the triangulation's
    own diagonals, and -- since M16a -- the second diagonal of every convex adjacent
    triangle pair, the cut a wire crosses when it rounds a triangle's apex without
    ever touching the diagonal (ADR 0014, and the toporouter postmortem §A.6, whose
    author named these "special cuts" in 2009). Both families together are still a
    *subset*: any segment between two obstacle vertices spanning more than one
    triangle pair is a cut nothing here charges. **So a clean result is evidence,
    not a proof.** It is a lower bound on congestion; what it reports is real, what
    it stays silent about may not be. Legality does not rest on it -- the stretcher
    builds each route inside free space that already excludes every other net, and
    :func:`aipcb.route.invariant.check_no_crossings` asks the finished board -- so
    the cost of the remaining gap is a route that fails or detours somewhere this
    said was fine, never a short circuit.
    """
    if not topologies:
        return
    stack = stack_for(netlist.layout, DEFAULT_COSTS)
    environment = extract_obstacles(board, edge_clearance=edge_clearance_for(netlist))
    classes = [
        netlist.net_classes[name]
        for name in sorted({net.net_class for net in netlist.nets.values()})
        if name in netlist.net_classes
    ] or [NetClass()]

    try:
        field_ = build_field(
            environment,
            stack,
            reference_clearance=max(c.clearance_mm for c in classes),
            reference_width=max(c.trace_width_mm for c in classes),
            via_radius=max(c.via_diameter_mm / 2 for c in classes),
            layout=netlist.layout,
            origin=netlist.layout.origin_mm if netlist.layout else (0.0, 0.0),
            special_cuts=True,
        )
    except FreeSpaceError:
        return

    owners: dict[tuple[str, str, int], list[str]] = {}
    for route in topologies:
        rules = rules_for(netlist, route.net)
        demand = rules.track_width + rules.clearance
        try:
            built = stretch_route(
                route,
                environment,
                _layer_geometry(environment, netlist, route, rules),
                rules,
                stack=stack,
            )
        except (StretchError, FreeSpaceError):
            continue  # reported as unrealizable by the per-route check
        for leg in built.legs:
            layer_field = field_.layers.get(leg.layer)
            if layer_field is None:
                continue
            for edge in layer_field.cuts_crossed(leg.points):
                layer_field.used[edge] += demand
                owners.setdefault(("diagonal", leg.layer, edge), []).append(route.net)
            for cut in layer_field.special_cuts_crossed(leg.points):
                layer_field.special_used[cut] += demand
                owners.setdefault(("special", leg.layer, cut), []).append(route.net)

    for layer, layer_field in sorted(field_.layers.items()):
        found = [
            ("diagonal", edge, layer_field.capacity[edge], layer_field.used[edge])
            for edge in layer_field.over_subscribed()
        ] + [
            (
                "special",
                cut,
                layer_field.special_capacity[cut],
                layer_field.special_used[cut],
            )
            for cut in layer_field.over_subscribed_special()
        ]
        for kind, index, capacity, used in found:
            nets = sorted(set(owners.get((kind, layer, index), ())))
            outcome.over_subscribed.append(
                {
                    "layer": layer,
                    "cut": kind,
                    "width_mm": round(capacity, 3),
                    "demand_mm": round(used, 3),
                    "nets": nets,
                }
            )
            where = (
                f"a corridor on {layer}"
                if kind == "diagonal"
                else f"the gap on {layer} they have to round a corner through"
            )
            report.error(
                "route-cut-over-subscribed",
                f"{', '.join(nets)} together need "
                f"{used:.2f} mm of {where} that is {capacity:.2f} mm wide",
                path=("layout", "routes"),
                hint="one of them has to go somewhere else: another layer, another "
                "side of the obstacle between them, or a different placement",
            )


def _layer_geometry(
    environment: RoutingEnvironment,
    netlist: Netlist,
    route: RouteTopology,
    rules: RouteRules,
) -> dict[str, LayerGeometry]:
    """One view of each layer the route uses, inflated by what this route needs.

    The route's own two pads are open to it -- it has to be able to land on them --
    and every other pad on its net is not, because a track that clips one on the way
    past leaves a copper sliver.
    """
    open_pads = frozenset(
        key
        for key in (
            environment.resolve_pad(route.from_),
            environment.resolve_pad(route.to),
        )
        if key
    )
    # Keepouts are part of the environment here for the same reason they are when
    # the board is routed: a check that ignores them says a route is realizable when
    # the router will refuse to build it.
    guarded = replace(environment, obstacles=dict(environment.obstacles))
    for obstacle in keepout_obstacles(
        netlist.layout, netlist.layout.origin_mm if netlist.layout else (0.0, 0.0)
    ):
        guarded.obstacles[obstacle.name] = obstacle

    geometry: dict[str, LayerGeometry] = {}
    for layer in dict.fromkeys(route.layers_used()):
        free = free_space(
            guarded,
            guarded.blocking(
                route.net,
                layer,
                clearance=rules.clearance,
                track_width=rules.track_width,
                clearance_of=lambda net: rules_for(netlist, net).clearance
                if net
                else 0.0,
                open_pads=open_pads,
            ),
            edge_margin=guarded.edge_clearance + rules.track_width / 2,
        )
        geometry[layer] = LayerGeometry(
            layer=layer, triangulation=triangulate_free(free), free=free
        )
    return geometry


def _structural_problems(
    route: RouteTopology,
    netlist: Netlist,
    net_pads: dict[str, set[str]],
    environment: object,
    report: Report,
    path: tuple[str | int, ...],
) -> bool:
    """Check what can be decided without geometry. Returns True if anything failed."""
    from aipcb.route.obstacles import RoutingEnvironment

    assert isinstance(environment, RoutingEnvironment)
    failed = False

    if route.net not in netlist.nets:
        report.error(
            "route-unknown-net",
            f"route topology names net {route.net!r}, which is not in the design",
            path=(*path, "net"),
            hint=f"nets available: {', '.join(sorted(netlist.nets)[:10])}",
        )
        return True

    pads = net_pads.get(route.net, set())
    for field, pad in (("from", route.from_), ("to", route.to)):
        if pad not in pads:
            report.error(
                "route-pad-not-on-net",
                f"route {route.key()}: {pad!r} is not a pad on net {route.net!r}",
                path=(*path, field),
                hint=f"pads on {route.net}: {', '.join(sorted(pads))}",
                net=route.net,
            )
            failed = True

    for position, waypoint in enumerate(route.passes):
        if isinstance(waypoint, ViaHop):
            continue
        if not isinstance(waypoint, Pass):  # pragma: no cover - discriminated union
            continue
        if environment.resolve_pad(waypoint.obstacle) is not None:
            continue
        if waypoint.obstacle in environment.obstacles:
            continue
        if waypoint.obstacle.startswith("via:"):
            named = {
                f"via:{hop.name}" for hop in route.via_hops if hop.name
            }
            if waypoint.obstacle in named:
                continue
        report.error(
            "route-unknown-obstacle",
            f"route {route.key()} passes {waypoint.obstacle!r}, which is not on the "
            "board",
            path=(*path, "passes", position, "obstacle"),
            hint="obstacles are pads ('U1.7'), components ('U1') or named vias",
            net=route.net,
        )
        failed = True

    layers = route.layers_used()
    stack = netlist.layout.stackup.copper_layers if netlist.layout else 2
    allowed = {"F.Cu", "B.Cu", *(f"In{i}.Cu" for i in range(1, stack - 1))}
    for layer in layers:
        if layer not in allowed:
            report.error(
                "route-unknown-layer",
                f"route {route.key()} uses layer {layer!r}, which this "
                f"{stack}-layer board does not have",
                path=(*path, "layer"),
                hint=f"layers available: {', '.join(sorted(allowed))}",
                net=route.net,
            )
            failed = True

    return failed

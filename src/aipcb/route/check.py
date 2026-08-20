"""Validating route topologies against a placed board -- milestone M7a.

A sketch can be well-formed and still impossible: it can name a pad that is not on
its net, ask to pass an obstacle that no longer exists, or describe a path around
one side of a part that placement has since made unreachable. This module answers
"is this sketch realizable *here*", where "here" is the current placement.

Realizability is not decided by inspection. The sketch is turned into a homotopy
class against a triangulation of the actual free space, exactly as the stretcher
would, and a class that cannot be built is one that does not exist.
"""

from __future__ import annotations

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.netlist import Netlist
from aipcb.route.model import Pass, RouteTopology, ViaHop
from aipcb.route.obstacles import extract_obstacles
from aipcb.route.plan import EDGE_CLEARANCE, rules_for
from aipcb.route.stretch import StretchError, stretch_route
from aipcb.route.triangulate import FreeSpaceError, build_triangulation

__all__ = ["RouteCheck", "check_routes"]


class RouteCheck:
    """The outcome of checking every route topology in a design."""

    __slots__ = ("realizable", "unrealizable")

    def __init__(self) -> None:
        self.realizable: list[RouteTopology] = []
        self.unrealizable: list[tuple[RouteTopology, str]] = []

    @property
    def ok(self) -> bool:
        return not self.unrealizable

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": len(self.realizable) + len(self.unrealizable),
            "realizable": [r.key() for r in self.realizable],
            "unrealizable": [
                {"route": r.key(), "reason": why} for r, why in self.unrealizable
            ],
        }


def check_routes(board: SNode, netlist: Netlist, report: Report) -> RouteCheck:
    """Check every ``layout.routes`` entry against the placed board."""
    outcome = RouteCheck()
    topologies = tuple(netlist.layout.routes) if netlist.layout else ()
    if not topologies:
        return outcome

    environment = extract_obstacles(board)
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
            blocking = environment.blocking(
                route.net,
                route.layer,
                clearance=rules.clearance,
                track_width=rules.track_width,
                clearance_of=lambda net: rules_for(netlist, net) .clearance if net else 0.0,
            )
            triangulation = build_triangulation(
                environment,
                blocking,
                edge_margin=EDGE_CLEARANCE + rules.track_width / 2,
            )
            stretch_route(route, environment, triangulation, rules)
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

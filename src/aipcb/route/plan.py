# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Routing a whole board: what to connect, where it goes, and on which layer.

The work divides in two, and keeping the halves apart is what makes M8 tractable.

**Negotiation is symbolic.** Which layer a connection uses, where its vias go and
which corridor it takes are decided on the shared layered field, where a route is a
set of subscriptions against cut capacities. Nothing there is geometry, so ripping a
route up and trying again costs nothing -- which is what lets the negotiation in
:mod:`aipcb.route.negotiate` run to convergence.

**Realization is geometric.** Once the topologies have settled, each connection is
tightened into copper in priority order, and each finished route becomes an obstacle
for the ones after it. That feedback is what keeps two routes sharing a corridor
from landing on top of each other: the cut criterion promises they *fit* side by
side, and the sequential rubber-band pass is what actually puts them there.

Explicit sketches from ``layout.routes`` are not negotiated -- the source said what
it wanted. They are realized first and their demand is charged to the field, so
everything else negotiates around copper that is already spoken for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.costs import DEFAULT_COSTS, CostModel
from aipcb.route.diffpair import DiffPair, find_pairs
from aipcb.route.emit import merge_overlapping_holes
from aipcb.route.fanout import FanoutResult, generate_fanout, settle_escapes
from aipcb.route.field import LayeredField, build_field
from aipcb.route.geometry import (
    DEFAULT_CONGESTION,
    class_for,
    class_name_for,
    edge_clearance_for,
    geometry_for,
    path_length,
    resample,
    rules_for,
    simplify,
    tighten_leg,
    track_obstacles,
    via_obstacles,
    with_copper,
)
from aipcb.route.graph import RoutePath, Terminal, search_path
from aipcb.route.invariant import (
    Crossing,
    SelfCrossing,
    check_no_crossings,
    check_no_self_crossings,
)
from aipcb.route.manual import manual_nets
from aipcb.route.model import RouteTopology
from aipcb.route.negotiate import Connection, Negotiation, default_priority, negotiate
from aipcb.route.obstacles import (
    EDGE_CLEARANCE,
    Obstacle,
    RoutingEnvironment,
    convex_hull,
    extract_obstacles,
    preserved_copper,
)
from aipcb.route.pairs import PairAudit, measure_skew, realize_pair
from aipcb.route.stack import RoutingStack, stack_for
from aipcb.route.stretch import (
    LayerGeometry,
    RoutedConnection,
    RouteRules,
    StretchError,
    StretchResult,
    Via,
    stretch_route,
)
from aipcb.route.timing import Stages, stage
from aipcb.route.transition import TransitionResult, generate_transitions
from aipcb.route.triangulate import FreeSpaceError

__all__ = [
    "DEFAULT_CONGESTION",
    "EDGE_CLEARANCE",
    "HANDOVER_KINDS",
    "RoutedBoard",
    "Unrouted",
    "route_board",
    "rules_for",
    "spanning_routes",
]

Point = tuple[float, float]

#: Why a connection was handed over rather than routed. ``over_complexity`` is the
#: honest one: the board has the corridors it has, and this net wanted more of them
#: than were left. The others say the search or the geometry failed on its own.
HANDOVER_KINDS = ("over_complexity", "no_path", "unrealizable")


@dataclass(frozen=True, slots=True)
class Unrouted:
    """A connection that could not be made, and why.

    This is a *hand-over*, not a silent gap. The router refuses to deliver marginal
    geometry -- everything it does lay is DRC-clean -- and says instead exactly which
    connection it could not make, where the board ran out of room, and which nets own
    the capacity that was contested. An agent can act on that; a human knows what to
    finish in KiCad, and M6 then treats the result as law.
    """

    net: str
    source: str
    target: str
    reason: str
    kind: str = "unrealizable"
    blocking: tuple[dict[str, object], ...] = ()
    """The cuts that were full, each with its layer, location, width and owners."""

    def key(self) -> str:
        return f"{self.net}/{self.source}>{self.target}"

    def to_dict(self) -> dict[str, object]:
        return {
            "net": self.net,
            "from": self.source,
            "to": self.target,
            "unrouted": self.kind,
            "reason": self.reason,
            "blocked_at": list(self.blocking),
        }


@dataclass(slots=True)
class RoutedBoard:
    """The outcome of routing a board."""

    connections: list[RoutedConnection] = field(default_factory=list)
    pairs: list[DiffPair] = field(default_factory=list)
    """Differential pairs that were routed as pairs."""
    skew: dict[str, float] = field(default_factory=dict)
    failed: list[Unrouted] = field(default_factory=list)
    contested_cuts: dict[str, tuple[dict[str, object], ...]] = field(default_factory=dict)
    """Per connection, the tightest corridors its negotiated path depended on."""
    negotiation: Negotiation | None = None
    pair_audits: list[PairAudit] = field(default_factory=list)
    """What M11d measured about every pair it tried, coupled or refused."""
    fanout: FanoutResult | None = None
    """The escape pattern laid before routing began, when a package asked for one."""
    transitions: TransitionResult | None = None
    """The pair via transitions laid before routing began (M11c)."""
    total_length: float = 0.0
    crossings: list[Crossing] = field(default_factory=list)
    """Places two nets' finished copper overlaps. Empty on any board worth having."""
    self_crossings: list[SelfCrossing] = field(default_factory=list)
    """Places one connection's copper meets its own (M16b, postmortem exposure E2)."""
    manual: list[str] = field(default_factory=list)
    """Nets the source declared `routing: manual`, which this router did not touch."""

    @property
    def ok(self) -> bool:
        return not self.failed

    def handed_over(self) -> list[dict[str, object]]:
        """Every connection the router refused, in a form an agent can act on."""
        return [f.to_dict() for f in sorted(self.failed, key=lambda f: f.key())]

    @property
    def routed(self) -> list[StretchResult]:
        """Every tightened leg, on every layer."""
        return [leg for connection in self.connections for leg in connection.legs]

    @property
    def vias(self) -> list[Via]:
        return [via for connection in self.connections for via in connection.vias]

    def summary(self) -> dict[str, object]:
        return {
            "routed": len(self.connections),
            "failed": len(self.failed),
            "length_mm": round(self.total_length, 3),
            "vias": len(self.vias),
            "layers": sorted({leg.layer for leg in self.routed}),
            "nets": sorted({c.net for c in self.connections}),
            "iterations": self.negotiation.iterations if self.negotiation else 0,
            "converged": bool(self.negotiation and self.negotiation.converged),
            "handed_over": self.handed_over(),
            "fanout": self.fanout.summary() if self.fanout is not None else None,
            "pairs": [audit.to_dict() for audit in self.pair_audits],
            "transitions": (
                self.transitions.summary() if self.transitions is not None else None
            ),
            "crossings": [c.describe() for c in self.crossings],
            "self_crossings": [c.describe() for c in self.self_crossings],
            "manual": list(self.manual),
        }


def spanning_routes(
    net: str, pads: list[str], centres: dict[str, Point], layer: str
) -> list[RouteTopology]:
    """Connect a net's pads with a minimum spanning tree.

    A star from one pad would be simpler and much longer. Prim's algorithm over
    Euclidean distance gives the shortest set of two-pad connections that ties the
    net together; ties break on pad name so the result never depends on ordering.
    """
    if len(pads) < 2:
        return []

    ordered = sorted(pads)
    inside = {ordered[0]}
    edges: list[tuple[str, str]] = []

    while len(inside) < len(ordered):
        best: tuple[float, str, str] | None = None
        for source in sorted(inside):
            for target in ordered:
                if target in inside:
                    continue
                distance = math.dist(centres[source], centres[target])
                candidate = (distance, source, target)
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            break
        _, source, target = best
        inside.add(target)
        edges.append((source, target))

    return [
        RouteTopology.model_validate(
            {"net": net, "from": source, "to": target, "layer": layer}
        )
        for source, target in edges
    ]


# ---------------------------------------------------------------------------
# obstacles fed back from finished copper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# routing a board
# ---------------------------------------------------------------------------


def route_board(
    board: SNode,
    netlist: Netlist,
    report: Report,
    *,
    layers: tuple[str, ...] | None = None,
    topologies: tuple[RouteTopology, ...] = (),
    congestion: float = DEFAULT_CONGESTION,
    costs: CostModel = DEFAULT_COSTS,
    manual_copper: bool = True,
    stages: Stages | None = None,
) -> RoutedBoard:
    """Route every net that needs it, across every layer the stackup allows.

    ``manual_copper`` decides whether copper already in the board is treated as a
    fixed obstacle. It is, always, except for the one pass the CLI makes to work out
    which of that copper is its own from a previous run.

    ``stages`` collects wall clock per phase when a benchmark is watching, and is
    ``None`` on every other path (M16c). Only the boundaries below are timed --
    never anything inside them.
    """
    outcome = RoutedBoard()
    stack = stack_for(netlist.layout, costs)
    base = extract_obstacles(board, edge_clearance=edge_clearance_for(netlist))

    manual = preserved_copper(board) if manual_copper else []
    for obstacle in manual:
        base.obstacles[obstacle.name] = obstacle
    if manual:
        report.info(
            "routing-around-manual-copper",
            f"{len(manual)} piece{'s' if len(manual) != 1 else ''} of copper already "
            "on the board are treated as fixed obstacles",
            hint="hand-routed tracks are preserved by the incremental build and "
            "routed around, never through",
        )

    # 0. Pattern generation, before anything is searched for. A dense package's
    #    escape is a known shape rather than a routing problem (ADR 0008): the
    #    generator lays the stubs and vias, that copper becomes a fixed obstacle,
    #    and the escape vias become the terminals the router sees in place of the
    #    package's own pads. Nothing downstream knows a fanout happened.
    placed: list[Obstacle] = []
    with stage(stages, "fanout"):
        fanout = generate_fanout(
            board, base, netlist, stack, report, congestion=congestion
        )
        fanout.apply(base)
    outcome.fanout = fanout
    for escape in fanout.connections:
        outcome.connections.append(escape)
        outcome.total_length += escape.copper_length

    #    The same regime, one tenant later: a declared pair via transition is two
    #    signal vias, its return vias, and two coupled segments for the router to
    #    route between (M11c).
    with stage(stages, "transitions"):
        transitions = generate_transitions(
            board, base, netlist, stack, report, congestion=congestion
        )
        transitions.apply(base)
    outcome.transitions = transitions
    for piece in transitions.connections:
        outcome.connections.append(piece)
        outcome.total_length += piece.copper_length

    classes = _classes_in_use(netlist)
    reference_clearance = max((c.clearance_mm for c in classes), default=0.2)
    reference_width = max((c.trace_width_mm for c in classes), default=0.25)
    via_radius = max((c.via_diameter_mm / 2 for c in classes), default=0.3)
    origin = netlist.layout.origin_mm if netlist.layout else (0.0, 0.0)

    try:
        with stage(stages, "field"):
            field_ = build_field(
                base,
                stack,
                reference_clearance=reference_clearance,
                reference_width=reference_width,
                via_radius=via_radius,
                layout=netlist.layout,
                origin=origin,
            )
    except FreeSpaceError as exc:
        report.warning("routing-impossible", str(exc))
        return outcome

    allowed = _allowed_layers(stack, layers, netlist, report)

    # 1. Explicit sketches. The source said what it wanted; it is not negotiable,
    #    and everything else negotiates around it.
    explicit: set[tuple[str, str, str]] = set()
    for route in topologies:
        explicit.add((route.net, route.from_, route.to))
        explicit.add((route.net, route.to, route.from_))

    # Declared-manual nets are the router's business only in that it must stay off
    # them. Excluded here, once, before anything is paired, negotiated or sketched:
    # a net skipped in one place and routed in another would be worse than not
    # having the field at all.
    declared_manual = set(manual_nets(netlist))
    # `manual_copper` is false only on the exploratory pass the CLI makes to learn
    # which copper on the board is its own from a previous run. Reporting on that
    # pass would say everything twice, so the notes below are the real pass's.
    speaking = manual_copper
    if declared_manual and speaking:
        report.info(
            "routing-manual-declared",
            f"{len(declared_manual)} net"
            f"{'s' if len(declared_manual) != 1 else ''} declared `routing: manual` "
            f"and left alone: {', '.join(sorted(declared_manual))}",
            hint="their copper comes from a hand route or an external router; "
            "`aipcb check` lists any that are still pending",
        )
    outcome.manual = sorted(declared_manual)

    # A pattern generator is not the router, and the two declarations can both be
    # true at once: `fanout:` asks for a specific escape shape on a specific package,
    # and `routing: manual` says the *router* stays off a net. A net that is both
    # gets its escape stub and nothing else. Honouring `routing: manual` here instead
    # would silently disable the other declaration, which is the worse of the two
    # surprises -- but neither is allowed to be a surprise, so it is said out loud.
    generated_on_manual = sorted(
        {
            piece.net
            for source in (outcome.fanout, outcome.transitions)
            if source is not None
            for piece in source.connections
            if piece.net in declared_manual
        }
    )
    if generated_on_manual and speaking:
        report.info(
            "manual-net-has-generated-pattern",
            f"{', '.join(generated_on_manual)} "
            f"{'are' if len(generated_on_manual) != 1 else 'is'} declared "
            "`routing: manual` and also carries copper from a declared pattern "
            "(a fanout escape or a pair via transition)",
            hint="a pattern generator is not the router: the source asked for that "
            "shape by name. The rest of the net is still yours",
        )

    pairs = [
        pair
        for pair in (
            *transitions.pairs,
            *find_pairs(netlist, base, report, skip=transitions.handled | declared_manual),
        )
        if pair.positive not in declared_manual and pair.negative not in declared_manual
    ]
    paired_nets = {n for pair in pairs for n in (pair.positive, pair.negative)}

    sketched = _route_sketches(
        [
            r
            for r in topologies
            if r.net not in paired_nets and r.net not in declared_manual
        ],
        base,
        netlist,
        stack,
        field_,
        placed,
        congestion,
        outcome,
        report,
    )

    # 2. Everything else negotiates.
    connections = _connections(
        netlist, base, field_, stack, allowed, pairs, explicit, sketched, congestion,
        declared_manual,
    )
    with stage(stages, "negotiate"):
        settled = negotiate(
            field_,
            connections,
            costs=costs,
            congestion_weight=congestion,
            report=report,
        )
    outcome.negotiation = settled
    outcome.contested_cuts = _blocking_cuts(field_, settled, connections)

    by_key = {c.key: c for c in connections}
    # Every negotiated path becomes geometry here, including the repairs a
    # failure triggers: the retry is part of what tightening cost, not a
    # separate stage, and a benchmark that split them would understate it.
    with stage(stages, "tighten"):
        for key, path in settled.paths.items():
            connection = by_key[key]
            try:
                if connection.pair is not None:
                    coupled = realize_pair(
                        connection, path, base, placed, netlist, stack, congestion,
                        report, outcome.pair_audits,
                    )
                    if coupled is None:
                        _route_pair_halves(
                            connection, base, netlist, stack, field_, placed,
                            congestion, outcome, report, allowed,
                        )
                        continue
                    outcome.pairs.append(connection.pair)  # type: ignore[arg-type]
                    _accept(outcome, placed, coupled, stack, trim=False)
                    continue
                realized = _realize(
                    connection, path, base, placed, netlist, stack, congestion
                )
            except (StretchError, FreeSpaceError) as exc:
                _retry(
                    connection, exc, base, placed, netlist, stack, field_,
                    congestion, outcome, report,
                )
                continue
            _accept(outcome, placed, realized, stack)

        for key, reason in sorted(settled.failed.items()):
            # The symbolic search itself found nothing, which is a different answer from
            # "a plan was found and would not tighten": there is no corridor at all.
            _retry(
                by_key[key],
                StretchError(reason),
                base, placed, netlist, stack, field_, congestion, outcome, report,
                kind="no_path",
            )

    # The fanout proposed an escape for every pad before any of this happened. Now
    # that the copper is real, the ones the router did not take up come back out.
    if fanout.terminals:
        _legs, vias = settle_escapes(outcome.connections, set(fanout.terminals))
        if vias:
            report.info(
                "fanout-escapes-settled",
                f"{vias} escape via{'s' if vias != 1 else ''} came back out: the "
                "router reached those pads without using the far layer",
                hint="the pattern generator proposes an escape per pad before "
                "anything is routed; a via that joins copper to nothing is not one",
            )

    holes = merge_overlapping_holes(outcome.connections)
    if holes:
        report.info(
            "vias-merged",
            f"{holes} via{'s' if holes != 1 else ''} of a net were drilled through "
            "the same copper as another and have been merged into one hole",
            hint="two barrels a fraction of a millimetre apart are one hole to a "
            "fabricator, and two overlapping drill hits to KiCad",
        )

    with stage(stages, "skew"):
        outcome.skew = measure_skew(
            outcome.pairs, outcome.connections, report, netlist
        )
    # The invariant, last: everything above builds copper inside free space that
    # already has the other nets removed from it, so this should never find
    # anything -- and it is exactly because it should never find anything that it
    # is worth asking. M11 shipped a repair pass that crossed two REFCLK tracks
    # and nothing noticed for a milestone and a half.
    with stage(stages, "invariant"):
        outcome.crossings = check_no_crossings(
            outcome.connections,
            report,
            barrel_layers={
                f"{a}/{b}": stack.barrel_span(a, b)
                for a in stack.copper
                for b in stack.copper
                if a != b
            },
        )
        # And the same question asked of one route rather than two.
        # `crossing_nets` skips same-net pairs by design, so until M16b nothing
        # anywhere looked at a connection's geometry against its own.
        outcome.self_crossings = check_no_self_crossings(outcome.connections, report)
    return outcome


def _accept(
    outcome: RoutedBoard,
    placed: list[Obstacle],
    realized: RoutedConnection | list[RoutedConnection],
    stack: RoutingStack,
    *,
    trim: bool = True,
) -> None:
    """Record finished copper, and feed it back as obstacles for what follows.

    ``trim`` is off for a differential pair. Its two halves are offsets of one
    tightened centre-line, and shortening either of them independently is exactly
    the thing that breaks the relationship the pair exists for.
    """
    for connection in realized if isinstance(realized, list) else [realized]:
        if trim:
            _join_existing_copper(connection, placed)
        outcome.connections.append(connection)
        outcome.total_length += connection.copper_length
        for leg in connection.legs:
            # The layer is part of the name because it is part of the identity: a
            # pair split by a via transition names its coupled leg after the same
            # two pair terminals on both layers, and an obstacle set keyed by name
            # alone kept only the second of them.
            placed.extend(
                track_obstacles(
                    leg,
                    f"track:{leg.net}@{leg.layer}/{leg.start}>{leg.end}",
                    leg.width / 2,
                )
            )
        for index, via in enumerate(connection.vias):
            placed.extend(
                via_obstacles(via, stack, f"via:{via.net}/{via.name}#{index}")
            )


#: How finely a finished route is sampled when looking for where it first leaves
#: copper its own net already has, in millimetres.
_OVERLAP_STEP = 0.1

def _join_existing_copper(
    connection: RoutedConnection, placed: list[Obstacle]
) -> None:
    """Stop a route laying copper on top of copper its own net already has.

    A net's own copper is not an obstacle -- it cannot be, or two connections of one
    net could never meet. The cost of that freedom is that a later connection will
    happily run the length of an earlier one before branching off, which is wasted
    copper and, where the two finally diverge, a wedge a few microns wide that KiCad
    reports as a sliver and a fabricator would rather not etch.

    Electrically the route is finished the moment it *touches* its net, so the
    duplicated part is not needed. Trimming it leaves the route starting on the
    existing copper, which is exactly where it was already connected.
    """
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    for leg in connection.legs:
        if leg.start.startswith("via:") or leg.end.startswith("via:"):
            # A leg that ends at a via must reach it. Trimming here would leave the
            # via connected to nothing on this layer, which KiCad reports as a
            # dangling via and a fabricator ships as an open circuit.
            continue
        existing = [
            ShapelyPolygon(o.polygon)
            for o in placed
            if o.net == leg.net
            and o.kind in ("track", "via")
            and (not o.layers or leg.layer in o.layers)
            and len(o.polygon) >= 3
        ]
        if not existing or len(leg.points) < 2:
            continue
        covered = unary_union(existing)
        samples = resample(leg.points, _OVERLAP_STEP)
        inside = [covered.covers(ShapelyPoint(p)) for p in samples]
        first = 0
        while first + 1 < len(inside) and inside[first + 1]:
            first += 1
        last = len(inside) - 1
        while last - 1 > first and inside[last - 1]:
            last -= 1
        if first == 0 and last == len(inside) - 1:
            continue
        leg.points = simplify(samples[first : last + 1])


#: How many corridors a hand-over names. The tightest few are what a reader acts
#: on; the rest of the path is where the route was never in trouble.
MAX_BLOCKING_CUTS = 3


def _blocking_cuts(
    field_: LayeredField,
    settled: Negotiation,
    connections: list[Connection],
) -> dict[str, tuple[dict[str, object], ...]]:
    """The corridors each connection depends on, worst first, with who else wants them.

    This is what turns "could not route" into something an agent can act on. A cut
    is a corridor across the free space with a width in millimetres; a route crossing
    it consumes its own width plus its clearance. When a connection has to be handed
    over, the useful answer is *where* the board ran out of room and *who else* was
    trying to get through there -- because the fix is always one of: move a part, add
    a signal layer, or let one of the named nets go somewhere else.

    Over-subscribed cuts come first, then the narrowest. A cut that is merely narrow
    is not yet a failure, but it is the place to look.
    """
    users: dict[tuple[str, int], list[str]] = {}
    by_key = {c.key: c for c in connections}
    for key, path in settled.paths.items():
        for layer, edges in path.crossings().items():
            for edge in edges:
                users.setdefault((layer, edge), []).append(key)

    out: dict[str, list[tuple[float, float, dict[str, object]]]] = {}
    for (layer, edge), keys in sorted(users.items()):
        layer_field = field_.layers.get(layer)
        if layer_field is None:  # pragma: no cover - the path came from this field
            continue
        point = layer_field.midpoints[edge]
        width = layer_field.capacity[edge]
        demand = layer_field.used[edge]
        record: dict[str, object] = {
            "layer": layer,
            "at": [round(point[0], 3), round(point[1], 3)],
            "width_mm": round(width, 3),
            "demand_mm": round(demand, 3),
            "over_subscribed": demand > width + 1e-9,
            "nets": sorted({by_key[k].net for k in keys if k in by_key}),
        }
        for key in keys:
            out.setdefault(key, []).append((-(demand - width), width, record))

    return {
        key: tuple(
            record for _, _, record in sorted(found, key=lambda item: item[:2])
        )[:MAX_BLOCKING_CUTS]
        for key, found in out.items()
    }


def _fail(
    outcome: RoutedBoard,
    report: Report,
    connection: Connection,
    exc: Exception,
    kind: str = "over_complexity",
) -> None:
    """Hand a connection over: no copper, and an explicit account of why not.

    Never a partial route and never marginal geometry. Everything this router does
    lay is DRC-clean, and what it cannot lay legally it refuses and reports -- which
    is the same bargain the differential-pair code already makes, applied to routing
    capacity in general (ADR 0008).
    """
    reason = getattr(exc, "message", str(exc))
    blocked = outcome.contested_cuts.get(connection.key, ())
    outcome.failed.append(
        Unrouted(
            net=connection.net,
            source=connection.source.name,
            target=connection.target.name,
            reason=reason,
            kind=kind,
            blocking=blocked,
        )
    )
    hint = getattr(exc, "hint", None)
    if blocked:
        worst = max(
            blocked,
            key=lambda cut: float(str(cut["demand_mm"])) - float(str(cut["width_mm"])),
        )
        owners = worst["nets"]
        contenders = ", ".join(str(n) for n in owners) if isinstance(owners, list) else ""
        hint = (
            f"the corridor on {worst['layer']} at {worst['at']} is "
            f"{worst['width_mm']} mm wide and {contenders} want "
            f"{worst['demand_mm']} mm of it. Move a part, add a signal layer, lower "
            "another net's priority, or route this one by hand -- a hand-routed net "
            "is preserved and treated as law on the next run"
        )
    report.warning(
        "route-handed-over",
        f"handing over {connection.net} from {connection.source.name} to "
        f"{connection.target.name} (unrouted: {kind}): {reason}",
        hint=hint
        or "move the parts apart, add a signal layer, or give the route an explicit "
        "topology under `layout.routes`",
        net=connection.net,
        unrouted=kind,
    )


def _pad_reference(pad_key: str) -> str:
    """``J1.6#3`` names a pad instance; the source format names pads ``J1.6``."""
    return pad_key.partition("#")[0]


def _classes_in_use(netlist: Netlist) -> list[NetClass]:
    used = {net.net_class for net in netlist.nets.values()}
    classes = [netlist.net_classes[name] for name in sorted(used) if name in netlist.net_classes]
    classes.append(NetClass())
    return classes


def _allowed_layers(
    stack: RoutingStack,
    requested: tuple[str, ...] | None,
    netlist: Netlist,
    report: Report,
) -> tuple[str, ...]:
    """Which layers this run may use: what was asked for, within what exists."""
    if requested is None:
        return stack.signal
    unknown = [name for name in requested if name not in stack.copper]
    if unknown:
        report.warning(
            "routing-unknown-layer",
            f"asked to route on {', '.join(unknown)}, which this "
            f"{len(stack.copper)}-layer board does not have",
            hint=f"layers available: {', '.join(stack.copper)}",
        )
    return tuple(name for name in requested if name in stack.copper) or stack.signal


# ---------------------------------------------------------------------------
# what to connect
# ---------------------------------------------------------------------------


def _connections(
    netlist: Netlist,
    base: RoutingEnvironment,
    field_: LayeredField,
    stack: RoutingStack,
    allowed: tuple[str, ...],
    pairs: list[DiffPair],
    explicit: set[tuple[str, str, str]],
    sketched: set[tuple[str, str]],
    congestion: float,
    declared_manual: set[str],
) -> list[Connection]:
    """Every connection the negotiation has to find a home for."""
    connections: list[Connection] = []
    paired_nets = {n for pair in pairs for n in (pair.positive, pair.negative)}

    for pair in pairs:
        connection = _pair_connection(pair, netlist, base, field_, stack, allowed)
        if connection is not None:
            connections.append(connection)

    for net in sorted(netlist.nets):
        if net in paired_nets or net in declared_manual:
            continue
        pads = sorted(key for key, pad_net in base.pad_nets.items() if pad_net == net)
        net_class = class_for(netlist, net)
        layers = _net_layers(stack, net_class, allowed)
        for candidate in spanning_routes(net, pads, base.pad_centres, allowed[0]):
            names = (
                _pad_reference(candidate.from_),
                _pad_reference(candidate.to),
            )
            if (net, *names) in explicit or (net, names[1], names[0]) in explicit:
                continue
            if (candidate.from_, candidate.to) in sketched:
                continue
            connection = _connection_for(
                net,
                candidate.from_,
                candidate.to,
                netlist,
                base,
                field_,
                layers,
                field_.reference_clearance,
            )
            if connection is not None:
                connections.append(connection)
    return connections


def _net_layers(
    stack: RoutingStack, net_class: NetClass, allowed: tuple[str, ...]
) -> tuple[str, ...]:
    """Which layers this class may use on this run.

    ``allowed`` is what the run was asked for, which by default is the stackup's
    signal layers. A class that names a *plane* under `prefer_layers` is asking for
    something outside that set on purpose, and gets it: opting in to a plane is the
    one way anything reaches one, and it would be a strange opt-in that the default
    layer list silently overrode.
    """
    permitted = stack.layers_for(net_class)
    chosen = tuple(
        name
        for name in permitted
        if name in allowed or name in net_class.prefer_layers
    )
    return chosen or permitted


def _connection_for(
    net: str,
    source_pad: str,
    target_pad: str,
    netlist: Netlist,
    base: RoutingEnvironment,
    field_: LayeredField,
    layers: tuple[str, ...],
    reference_clearance: float,
) -> Connection | None:
    net_class = class_for(netlist, net)
    name = class_name_for(netlist, net)
    source = _terminal(field_, base, source_pad, layers)
    target = _terminal(field_, base, target_pad, layers)
    if source is None or target is None:
        return None
    return Connection(
        key=f"{net}/{source_pad}>{target_pad}",
        net=net,
        net_class_name=name,
        net_class=net_class,
        source=source,
        target=target,
        demand=net_class.trace_width_mm + net_class.clearance_mm,
        # The widest clearance on the board, not this net's: KiCad enforces the
        # larger of the two, and a via sitting at its own class's figure from a pad
        # whose class asks for more is a violation by exactly the difference.
        via_radius=net_class.via_diameter_mm / 2 + reference_clearance,
        layers=layers,
        priority=default_priority(name, net_class, pair=False),
        policy=net_class.rip_up,
    )


def _pair_connection(
    pair: DiffPair,
    netlist: Netlist,
    base: RoutingEnvironment,
    field_: LayeredField,
    stack: RoutingStack,
    allowed: tuple[str, ...],
) -> Connection | None:
    """A differential pair, as one object in the graph.

    One path, twice the appetite. Routing the two halves separately and hoping they
    end up parallel is not a pair -- the impedance comes from the coupling, and the
    coupling comes from the two halves having been routed as one thing.
    """
    net_class = class_for(netlist, pair.positive)
    name = class_name_for(netlist, pair.positive)
    layers = _net_layers(stack, net_class, allowed)
    if pair.layer is not None and pair.layer in stack.layers_for(net_class):
        # A transition put this segment on one layer and the vias at its ends are
        # what let it change; letting the search pick another layer would be
        # letting it ignore the transition.
        layers = (pair.layer,)
    source = _pair_terminal(field_, base, pair.starts, layers, f"{pair.label()}<")
    target = _pair_terminal(field_, base, pair.ends, layers, f"{pair.label()}>")
    if source is None or target is None:
        return None
    return Connection(
        key=f"pair:{pair.label()}",
        net=pair.positive,
        net_class_name=name,
        net_class=net_class,
        source=source,
        target=target,
        demand=pair.corridor + net_class.clearance_mm,
        via_radius=net_class.via_diameter_mm / 2 + field_.reference_clearance,
        layers=layers,
        priority=default_priority(name, net_class, pair=True),
        policy=net_class.rip_up,
        # A pair stays on one layer. Both halves would have to change layer
        # together to keep the gap, and a paired via column is not two vias at the
        # pair's own pitch -- a 0.6 mm via at a 0.54 mm pitch is one piece of
        # copper. The halves have to splay out to via pitch and back, which is a
        # distinct pattern with its own impedance discontinuity, and it is not
        # built. A pair that cannot be routed on one layer says so instead
        # (ADR 0007).
        max_vias=0,
        pair=pair,
    )


def _terminal(
    field_: LayeredField,
    base: RoutingEnvironment,
    pad: str,
    layers: tuple[str, ...],
) -> Terminal | None:
    centre = base.pad_centres.get(pad)
    if centre is None:
        return None
    escapes: list[tuple[str, Point, int]] = []
    for layer in layers:
        if not _pad_reaches(base, pad, layer):
            continue
        layer_field = field_.layers.get(layer)
        if layer_field is None:
            continue
        # Where the pad's own centre is visible in this field -- which it is
        # whenever the field was built for this net -- only escapes in the same
        # piece of free space count. A pad can perfectly well have clear air around
        # it that is walled off from the pad itself, and an escape over the wall is
        # a route to somewhere the copper cannot follow.
        home = None
        inside = layer_field.triangulation.locate(centre)
        if inside is not None:
            home = layer_field.triangulation.component(inside)
        for point, triangle in field_.escapes(base, pad, layer):
            if home is not None and layer_field.triangulation.component(triangle) != home:
                continue
            escapes.append((layer, point, triangle))
    if not escapes:
        return None
    return Terminal(name=pad, centre=centre, escapes=tuple(sorted(escapes)))


def _pair_terminal(
    field_: LayeredField,
    base: RoutingEnvironment,
    pads: tuple[str, str],
    layers: tuple[str, ...],
    key: str,
) -> Terminal | None:
    """One end of a pair: the two pads together, as a single thing to escape from."""
    first = base.obstacles.get(pads[0])
    second = base.obstacles.get(pads[1])
    centres = [base.pad_centres.get(p) for p in pads]
    if first is None or second is None or any(c is None for c in centres):
        return None
    hull = convex_hull(tuple(first.polygon) + tuple(second.polygon))
    midpoint = (
        (centres[0][0] + centres[1][0]) / 2,  # type: ignore[index]
        (centres[0][1] + centres[1][1]) / 2,  # type: ignore[index]
    )
    escapes: list[tuple[str, Point, int]] = []
    for layer in layers:
        if not all(_pad_reaches(base, p, layer) for p in pads):
            continue
        escapes.extend(
            (layer, point, triangle)
            for point, triangle in field_.escapes_from(hull, layer, key)
        )
    if not escapes:
        return None
    return Terminal(name=key, centre=midpoint, escapes=tuple(sorted(escapes)))


def _pad_reaches(base: RoutingEnvironment, pad: str, layer: str) -> bool:
    layers = base.pad_layers.get(pad, frozenset())
    return not layers or layer in layers or "*.Cu" in layers


# ---------------------------------------------------------------------------
# realization
# ---------------------------------------------------------------------------


def _realize(
    connection: Connection,
    path: RoutePath,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    congestion: float,
    open_pads: frozenset[str] | None = None,
) -> RoutedConnection:
    """Turn a negotiated path into copper, one leg at a time."""
    rules = rules_for(netlist, connection.net, congestion)
    realized = RoutedConnection(
        net=connection.net,
        start=connection.source.name,
        end=connection.target.name,
    )
    names = _leg_names(connection, path)
    geometries: list[LayerGeometry] = []
    for index, leg in enumerate(path.legs):
        geometry = geometry_for(
            base,
            placed,
            netlist,
            connection.net,
            leg.layer,
            rules,
            congestion,
            open_pads=open_pads
            if open_pads is not None
            else frozenset({connection.source.name, connection.target.name}),
        )
        start = base.pad_centres[connection.source.name] if index == 0 else leg.start
        end = (
            base.pad_centres[connection.target.name]
            if index == len(path.legs) - 1
            else leg.end
        )
        points, crossings = tighten_leg(
            start, end, list(leg.guides), geometry, rules, f"route {connection.key}"
        )
        geometries.append(geometry)
        realized.legs.append(
            StretchResult(
                net=connection.net,
                layer=leg.layer,
                points=points,
                width=rules.track_width,
                crossings=crossings,
                start=names[index],
                end=names[index + 1],
            )
        )
    for index, via in enumerate(path.vias):
        # The site was chosen against the shared field, before any copper existed.
        # By now some has, and a via that no longer clears it has to be found a new
        # home -- which is what the repair pass does, on a field that knows where
        # the copper actually went.
        for geometry in geometries[index : index + 2]:
            if not _via_clears(via.point, geometry, rules):
                raise StretchError(
                    f"the via {connection.net} needs on {geometry.layer} no longer "
                    "clears the copper that has been laid since it was chosen",
                    hint="the connection is re-routed against the board as it "
                    "stands",
                )
        realized.vias.append(
            Via(
                net=connection.net,
                point=via.point,
                from_layer=via.from_layer,
                to_layer=via.to_layer,
                diameter=rules.via_diameter,
                drill=rules.via_drill,
                kind=stack.via_type(via.from_layer, via.to_layer) or "through",
                name=f"{connection.key}#{index}",
            )
        )
        realized.barrel_length += stack.barrel_length(via.from_layer, via.to_layer)

    _relax_vias(realized, geometries, [list(leg.guides) for leg in path.legs], rules)
    return realized


#: Where a via is tried when it is being pulled towards its two neighbours: fractions
#: along the straight line between them, and partial moves from where it is towards
#: the nearest point of that line. Coarse on purpose -- the search picked the pocket,
#: and this only has to find the good corner of it.
_RELAX_ALONG = (0.5, 0.35, 0.65, 0.2, 0.8)
_RELAX_TOWARDS = (1.0, 0.75, 0.5, 0.25)


def _relax_vias(
    connection: RoutedConnection,
    geometries: list[LayerGeometry],
    guides: list[list[Point]],
    rules: RouteRules,
) -> None:
    """Let each via settle where its two rubber bands would pull it.

    The search chose the via's *site* from a discrete set -- triangle incentres and a
    coarse grid -- because optimising a continuous coordinate inside a combinatorial
    search buys nothing that this pass cannot buy afterwards, more cheaply. What it
    leaves behind is a via sitting somewhere reasonable in the right pocket while the
    two legs meeting there both bend to reach it. On the four-layer example that
    detour was 11 mm of copper across five vias, one of them alone worth 5.8 mm.

    So each via is pulled towards the straight line between its neighbouring corners,
    the two legs are re-tightened against it, and the move is kept only if the pair of
    legs came out *shorter* and both are still legal. Bounded, monotone, and unable to
    make a board worse: the worst case is that nothing moves.
    """
    for index, via in enumerate(connection.vias):
        if index + 1 >= len(connection.legs):
            break
        before = connection.legs[index]
        after = connection.legs[index + 1]
        if len(before.points) < 2 or len(after.points) < 2:
            continue
        anchor, onward = before.points[-2], after.points[1]

        best = before.length + after.length
        for candidate in _relaxed_positions(via.point, anchor, onward):
            if not _via_clears(candidate, geometries[index], rules) or not _via_clears(
                candidate, geometries[index + 1], rules
            ):
                continue
            try:
                head, head_crossings = tighten_leg(
                    before.points[0], candidate, guides[index],
                    geometries[index], rules, f"via relax {connection.net}",
                )
                tail, tail_crossings = tighten_leg(
                    candidate, after.points[-1], guides[index + 1],
                    geometries[index + 1], rules, f"via relax {connection.net}",
                )
            except (StretchError, FreeSpaceError):
                continue
            moved = path_length(head) + path_length(tail)
            if moved >= best - 1e-9:
                continue
            best = moved
            before.points, before.crossings = head, head_crossings
            after.points, after.crossings = tail, tail_crossings
            connection.vias[index] = replace(via, point=candidate)
            via = connection.vias[index]


def _relaxed_positions(
    via: Point, anchor: Point, onward: Point
) -> list[Point]:
    """Where to try putting a via, nearest-to-home first."""
    nearest = _closest_on_segment(via, anchor, onward)
    towards = [
        (via[0] + (nearest[0] - via[0]) * step, via[1] + (nearest[1] - via[1]) * step)
        for step in _RELAX_TOWARDS
    ]
    along = [
        (
            anchor[0] + (onward[0] - anchor[0]) * fraction,
            anchor[1] + (onward[1] - anchor[1]) * fraction,
        )
        for fraction in _RELAX_ALONG
    ]
    return [*towards, *along]


def _closest_on_segment(point: Point, a: Point, b: Point) -> Point:
    span = (b[0] - a[0], b[1] - a[1])
    length = span[0] ** 2 + span[1] ** 2
    if length < 1e-18:
        return a
    t = ((point[0] - a[0]) * span[0] + (point[1] - a[1]) * span[1]) / length
    t = min(1.0, max(0.0, t))
    return (a[0] + span[0] * t, a[1] + span[1] * t)


def _retry(
    connection: Connection,
    exc: Exception,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    field_: LayeredField,
    congestion: float,
    outcome: RoutedBoard,
    report: Report,
    kind: str = "over_complexity",
) -> None:
    """Repair a connection the moment it fails, not once the board is full.

    Order matters more than it looks. Everything realized after this connection
    becomes an obstacle to it, so a repair deferred to the end is a repair attempted
    against the most crowded board there will ever be -- and on the `diff-pair`
    example that was the difference between finding a corridor and finding a pad
    walled in by two tracks that were laid while the repair waited its turn.
    """
    if not _repair(
        connection, base, placed, netlist, stack, field_, congestion, outcome, report
    ):
        _fail(outcome, report, connection, exc, kind)


def _repair(
    connection: Connection,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    field_: LayeredField,
    congestion: float,
    outcome: RoutedBoard,
    report: Report,
) -> bool:
    """One more try for a connection the shared field could not place.

    The retry is on a field built for *this net alone*: its own clearance, its own
    width, its own pads not counted as obstacles, and every piece of copper realized
    so far included. The free space that produces is exactly the free space the
    stretcher will tighten in, so a path found here is realizable by construction
    rather than by approximation -- which is the whole point of paying for a second
    field only where the first one failed.

    Two pads of one net sitting inside each other's clearance is the common case:
    a receptacle's four shield tabs are all on GND and all in each other's way, and
    on the shared field, where every pad blocks, there is no corridor between them at
    all. On the net's own field they are simply two points with nothing in between.
    """
    if connection.pair is not None:
        return False
    rules = rules_for(netlist, connection.net, congestion)
    demand = rules.track_width + rules.clearance

    environment = with_copper(base, placed)

    def blocking_for(layer: str) -> list[Obstacle]:
        return environment.blocking(
            connection.net,
            layer,
            clearance=rules.clearance,
            track_width=rules.track_width,
            clearance_of=lambda net: rules_for(netlist, net, congestion).clearance
            if net
            else 0.0,
            # Permissive on purpose. The first pass keeps a route clear of its own
            # net's other pads, because clipping one tangentially leaves a copper
            # crescent that reads as a sliver. Here that has already cost the
            # connection its route, and running through a pad of its own net is a
            # far better outcome than not connecting at all -- which is exactly the
            # situation a receptacle's four shield tabs create.
            open_pads=frozenset(base.pad_centres),
        )

    try:
        private = build_field(
            environment,
            stack,
            reference_clearance=rules.clearance,
            reference_width=rules.track_width,
            via_radius=connection.via_radius,
            layout=netlist.layout,
            origin=netlist.layout.origin_mm if netlist.layout else (0.0, 0.0),
            blocking_for=blocking_for,
            capacity_offset=demand,
            inflation=rules.clearance + rules.track_width / 2,
        )
    except FreeSpaceError:
        return False

    source = _terminal(private, environment, connection.source.name, connection.layers)
    target = _terminal(private, environment, connection.target.name, connection.layers)
    if source is None or target is None:
        return False
    path = search_path(private, source, target, connection.rules(1.0, congestion))
    if path is None:
        return False
    try:
        realized = _realize(
            replace(connection, source=source, target=target),
            path,
            base,
            placed,
            netlist,
            stack,
            congestion,
            open_pads=frozenset(base.pad_centres),
        )
    except (StretchError, FreeSpaceError):
        return False
    _accept(outcome, placed, realized, stack)
    # The repair searched its own private field, so its demand was never booked
    # against the shared one. Charging it here is what keeps the congestion
    # figures -- and every route that negotiates or searches after it -- looking
    # at a board that has this copper on it.
    _charge(field_, realized, netlist, congestion)
    report.info(
        "route-repaired",
        f"{connection.net} from {connection.source.name} to "
        f"{connection.target.name} was routed on a second pass, against the board as "
        "it actually stands",
        net=connection.net,
    )
    return True


def _via_clears(point: Point, geometry: LayerGeometry, rules: RouteRules) -> bool:
    """Whether a via of this size fits at a point, on this layer, right now.

    ``geometry``'s obstacles are already grown by the route's clearance and half its
    width, so the room a via needs beyond that boundary is its radius less that half
    width -- charging the clearance twice would rule out every via on a tight board.
    """
    from shapely.geometry import Point as ShapelyPoint

    free = geometry.free
    if free is None:
        return True
    at = ShapelyPoint(point)
    if not free.covers(at):
        return False
    needed = max(0.0, rules.via_diameter / 2 - rules.track_width / 2)
    return bool(free.boundary.distance(at) >= needed)


def _leg_names(connection: Connection, path: RoutePath) -> list[str]:
    """What each leg's two ends are called, for deterministic UUIDs."""
    names = [connection.source.name]
    names.extend(f"via:{connection.key}#{index}" for index in range(len(path.vias)))
    names.append(connection.target.name)
    return names


def _route_sketches(
    topologies: list[RouteTopology],
    base: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    field_: LayeredField,
    placed: list[Obstacle],
    congestion: float,
    outcome: RoutedBoard,
    report: Report,
) -> set[tuple[str, str]]:
    """Realize the sketches the source wrote, and charge what they use.

    They go first and they are not negotiable. Charging their crossings to the
    shared field afterwards is what makes them visible to the negotiation: a net
    that would otherwise pick the corridor a declared route is sitting in sees it as
    already spoken for.
    """
    done: set[tuple[str, str]] = set()
    for route in topologies:
        rules = rules_for(netlist, route.net, congestion)
        endpoints = frozenset(
            key
            for key in (base.resolve_pad(route.from_), base.resolve_pad(route.to))
            if key
        )
        try:
            geometry = {
                layer: geometry_for(
                    base,
                    placed,
                    netlist,
                    route.net,
                    layer,
                    rules,
                    congestion,
                    open_pads=endpoints,
                )
                for layer in dict.fromkeys(route.layers_used())
            }
            realized = stretch_route(route, base, geometry, rules, stack=stack)
        except (StretchError, FreeSpaceError) as exc:
            reason = getattr(exc, "message", str(exc))
            outcome.failed.append(
                Unrouted(
                    net=route.net,
                    source=route.from_,
                    target=route.to,
                    reason=reason,
                    kind="unrealizable",
                )
            )
            report.warning(
                "route-handed-over",
                f"handing over {route.net} from {route.from_} to {route.to} "
                f"(unrouted: unrealizable): {reason}",
                hint=getattr(exc, "hint", None)
                or "pass the obstacle on the other side, add a via hop, or move the "
                "parts apart",
                net=route.net,
            )
            continue
        _accept(outcome, placed, realized, stack)
        _charge(field_, realized, netlist, congestion)
        done.add((realized.start, realized.end))
        done.add((realized.end, realized.start))
    return done


def _charge(
    field_: LayeredField,
    connection: RoutedConnection,
    netlist: Netlist,
    congestion: float,
) -> None:
    """Book a finished route's demand against the cuts its copper actually crosses."""
    rules = rules_for(netlist, connection.net, congestion)
    demand = rules.track_width + rules.clearance
    for leg in connection.legs:
        layer_field = field_.layers.get(leg.layer)
        if layer_field is None:
            continue
        for edge in layer_field.cuts_crossed(leg.points):
            layer_field.used[edge] += demand


# ---------------------------------------------------------------------------
# differential pairs
# ---------------------------------------------------------------------------


def _route_pair_halves(
    connection: Connection,
    base: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    field_: LayeredField,
    placed: list[Obstacle],
    congestion: float,
    outcome: RoutedBoard,
    report: Report,
    allowed: tuple[str, ...],
) -> None:
    """Route a refused pair's two halves as ordinary nets, which is worse but honest."""
    pair = connection.pair
    assert isinstance(pair, DiffPair)
    for net, ends in (
        (pair.positive, (pair.starts[0], pair.ends[0])),
        (pair.negative, (pair.starts[1], pair.ends[1])),
    ):
        source = base.resolve_pad(ends[0]) or ends[0]
        target = base.resolve_pad(ends[1]) or ends[1]
        layers = _net_layers(stack, class_for(netlist, net), allowed)
        half = _connection_for(
            net, source, target, netlist, base, field_, layers,
            field_.reference_clearance,
        )
        if half is None:
            continue
        from aipcb.route.graph import search_path

        path = search_path(field_, half.source, half.target, half.rules(1.0, congestion))
        if path is None:
            _fail(
                outcome, report, half,
                StretchError("no path on any allowed layer"), "no_path",
            )
            continue
        try:
            realized = _realize(
                half, path, base, placed, netlist, stack, congestion
            )
        except (StretchError, FreeSpaceError) as exc:
            _fail(outcome, report, half, exc)
            continue
        field_.add_usage(path.crossings(), half.demand)
        _accept(outcome, placed, realized, stack)


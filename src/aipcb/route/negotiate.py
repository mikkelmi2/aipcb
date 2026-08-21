"""Negotiated congestion: every net routes, the contested corridors get expensive.

M7c routed nets in a fixed order and fed each finished route back as geometry. That
works until two nets want the same corridor, at which point whichever went first
wins and the other fails -- and the order was decided by a heuristic, so the board
that comes out depends on a guess made before anything was known.

PathFinder (McMurchie & Ebeling, 1995) replaces the guess with a negotiation. Every
net is routed, and nets are *allowed* to share a corridor illegally. Then the price
of over-used corridors goes up, the nets that lost the argument are ripped up and
re-routed, and the process repeats. A net gives up a corridor only when the corridor
is genuinely contested, and the amount of contention -- not the routing order --
decides who keeps it.

Two things make this affordable here. Ripping up costs nothing, because a route is a
set of subscriptions on cut capacities rather than a piece of copper. And the
capacity criterion is local, so "has this converged" is a scan over the cuts.

Priority is what the source gets to say about the outcome. It orders the first pass,
and it decides who keeps a corridor when two nets want it: on each contested cut the
strongest net stays and the others move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aipcb.diagnostics import Report
from aipcb.model.layout import NetClass
from aipcb.route.costs import DEFAULT_COSTS, CostModel
from aipcb.route.field import LayeredField
from aipcb.route.graph import RoutePath, SearchRules, Terminal, search_path

__all__ = [
    "Connection",
    "Negotiation",
    "default_priority",
    "negotiate",
    "rip_up_weight",
]

#: What priority a net class gets when the source does not say. These are M7's
#: measured ordering, re-expressed as priorities: a pair escaping a fine-pitch part
#: has almost no freedom and must choose first, while a ground hop between two
#: capacitors has plenty and can go last. Expressing the heuristic *as* a default
#: priority rather than as separate code means there is one mechanism, and a design
#: can override any of it by saying so.
CLASS_PRIORITY: dict[str, int] = {
    "diff_pair": 80,
    "usb": 80,
    "high_speed": 75,
    "clock": 75,
    "analog": 65,
    "power": 60,
    "ground": 55,
    "signal": 50,
}

#: The priority a matched or paired connection gets whatever its class says, unless
#: the class states one explicitly.
PAIR_PRIORITY = 80

#: The default for a class nobody has an opinion about.
BASE_PRIORITY = 50

#: How many passes without fewer over-subscribed cuts before the negotiation gives
#: up. Three, because PathFinder's present-congestion term rises each pass and
#: sometimes needs two to dislodge a stubborn pair of nets -- but a board that has
#: not improved in three is over-subscribed rather than unlucky.
PATIENCE = 3


def default_priority(net_class_name: str, net_class: NetClass, *, pair: bool) -> int:
    """The priority this net routes at."""
    if net_class.priority is not None:
        return net_class.priority
    if pair:
        return PAIR_PRIORITY
    return CLASS_PRIORITY.get(net_class_name, BASE_PRIORITY)


def rip_up_weight(policy: str, priority: int, costs: CostModel) -> float:
    """How hard it is to make this connection move.

    A `protected` net is disturbed only when the alternative is failing to route
    something else; a `never` net only as the last thing tried before declaring the
    board unroutable, because reporting failure without having tried is worse than
    trying. Neither is infinite, and the failure report names whichever one blocked.
    """
    if policy == "never":
        return priority * costs.rip_up_never
    if policy == "protected":
        return priority * costs.rip_up_protected
    return float(priority)


@dataclass(slots=True)
class Connection:
    """One thing to route: a pair of terminals and the rules that govern it."""

    key: str
    net: str
    net_class_name: str
    net_class: NetClass
    source: Terminal
    target: Terminal
    demand: float
    via_radius: float
    layers: tuple[str, ...]
    priority: int = BASE_PRIORITY
    policy: str = "normal"
    max_vias: int = 4
    """How many layer changes this connection may make."""
    pair: object | None = None
    """The :class:`~aipcb.route.diffpair.DiffPair` this is, if it is one."""

    @property
    def span(self) -> float:
        return math.dist(self.source.centre, self.target.centre)

    def rules(self, present: float, congestion_weight: float) -> SearchRules:
        return SearchRules(
            net=self.net,
            net_class=self.net_class,
            demand=self.demand,
            via_radius=self.via_radius,
            layers=self.layers,
            present=present,
            congestion_weight=congestion_weight,
            max_vias=self.max_vias,
        )


@dataclass(slots=True)
class Negotiation:
    """What the negotiation settled on."""

    paths: dict[str, RoutePath] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = False
    log: list[dict[str, object]] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    """Protected or never-rip nets that were still holding contested cuts at the end."""


def negotiate(
    field_: LayeredField,
    connections: list[Connection],
    *,
    costs: CostModel = DEFAULT_COSTS,
    congestion_weight: float = 1.0,
    report: Report | None = None,
) -> Negotiation:
    """Route everything, then argue about the corridors until nobody is over budget."""
    outcome = Negotiation()
    if not connections:
        outcome.converged = True
        return outcome

    by_key = {c.key: c for c in connections}
    order = sorted(connections, key=lambda c: (-c.priority, -_difficulty(field_, c), c.key))
    pending = [c.key for c in order]
    best_overuse = math.inf
    stale = 0

    for iteration in range(max(costs.iterations, 1)):
        present = costs.congestion_present * costs.congestion_growth**iteration
        for key in pending:
            connection = by_key[key]
            existing = outcome.paths.pop(key, None)
            if existing is not None:
                field_.remove_usage(existing.crossings(), connection.demand)
            path = search_path(
                field_,
                connection.source,
                connection.target,
                connection.rules(present, congestion_weight),
                costs,
            )
            if path is None:
                outcome.failed[key] = (
                    "no path exists on any allowed layer: the pads are walled in by "
                    "other parts' clearances"
                )
                continue
            outcome.failed.pop(key, None)
            outcome.paths[key] = path
            field_.add_usage(path.crossings(), connection.demand)

        congested = field_.congested()
        overuse = sum(len(edges) for edges in congested.values())
        outcome.iterations = iteration + 1
        outcome.log.append(
            {
                "iteration": iteration + 1,
                "present": round(present, 3),
                "routed": len(outcome.paths),
                "unrouted": len(outcome.failed),
                "over_subscribed_cuts": overuse,
                "rerouted": len(pending),
            }
        )
        if not congested:
            outcome.converged = True
            break

        # Negotiation that has stopped making progress is not going to start.
        # A board can be genuinely over-subscribed -- a connector fan-out with more
        # pins than escapes -- and grinding through the full iteration budget to
        # discover that costs seconds and tells nobody anything new.
        if overuse < best_overuse - 1e-9:
            best_overuse, stale = overuse, 0
        else:
            stale += 1
            if stale >= PATIENCE:
                break

        pending = _losers(field_, congested, by_key, outcome, costs)
        if not pending:
            break
        field_.age(costs.congestion_history)

    _order_paths(outcome, order)
    if not outcome.converged:
        outcome.blocked_by = _blockers(field_, by_key, outcome)
        if report is not None:
            _report_failure(field_, outcome, report)
    elif report is not None and outcome.iterations > 1:
        report.info(
            "routing-negotiated",
            f"congestion settled after {outcome.iterations} negotiation "
            f"{'passes' if outcome.iterations != 1 else 'pass'}",
            hint="each pass re-routes the nets that lost a contested corridor",
        )
    return outcome


def _order_paths(outcome: Negotiation, order: list[Connection]) -> None:
    """Put the settled paths back into routing order, not rip-up order.

    The dict has been mutated in whatever sequence the negotiation touched it, and
    geometry is realized in the order this returns -- so it has to be the priority
    order, or a high-priority net would be pushed aside by copper from a low-priority
    one that merely happened to be re-routed later.
    """
    ordered = {c.key: outcome.paths[c.key] for c in order if c.key in outcome.paths}
    outcome.paths = ordered


def _difficulty(field_: LayeredField, connection: Connection) -> float:
    """How little freedom this connection has: length times congestion.

    Congestion, before anything has been routed, is what the *geometry* already
    says: the narrowest cut a route leaving either pad has to get through. A pair
    escaping a 0.65 mm-pitch receptacle scores enormously and chooses first; a
    ground hop between two capacitors in open board scores near nothing and goes
    last. That is M7's measured ordering, arrived at from the numbers rather than
    from a table of class names.
    """
    narrowest = math.inf
    for terminal in (connection.source, connection.target):
        for layer, _point, triangle in terminal.escapes:
            layer_field = field_.layers.get(layer)
            if layer_field is None:
                continue
            for edge in layer_field.triangulation.by_triangle.get(triangle, ()):
                narrowest = min(narrowest, layer_field.capacity[edge])
    if not math.isfinite(narrowest) or narrowest <= 0:
        return connection.span * 1e3
    return connection.span * connection.demand / narrowest


def _losers(
    field_: LayeredField,
    congested: dict[str, list[int]],
    by_key: dict[str, Connection],
    outcome: Negotiation,
    costs: CostModel,
) -> list[str]:
    """Who has to move: everyone on a contested cut except its strongest user.

    This is where priority earns its keep. On each over-subscribed cut the net that
    is hardest to rip up keeps its place and the rest re-route, so a `protected`
    high-priority net owns its corridor and low-priority traffic detours around it
    -- which is exactly what a person does when they route a clock first and then
    everything else.
    """
    users: dict[tuple[str, int], list[str]] = {}
    for key, path in outcome.paths.items():
        for layer, edges in path.crossings().items():
            over = set(congested.get(layer, ()))
            for edge in edges:
                if edge in over:
                    users.setdefault((layer, edge), []).append(key)

    moving: set[str] = set()
    for cut in sorted(users):
        contenders = sorted(set(users[cut]))
        if len(contenders) == 1:
            moving.update(contenders)
            continue
        winner = max(
            contenders,
            key=lambda k: (
                rip_up_weight(by_key[k].policy, by_key[k].priority, costs),
                k,
            ),
        )
        moving.update(c for c in contenders if c != winner)

    ordered = sorted(
        moving,
        key=lambda k: (-by_key[k].priority, -_difficulty(field_, by_key[k]), k),
    )
    return ordered


def _blockers(
    field_: LayeredField, by_key: dict[str, Connection], outcome: Negotiation
) -> list[str]:
    """Which protected or never-rip nets are still sitting on contested cuts."""
    congested = field_.congested()
    names: set[str] = set()
    for key, path in outcome.paths.items():
        connection = by_key[key]
        if connection.policy == "normal":
            continue
        for layer, edges in path.crossings().items():
            if set(edges) & set(congested.get(layer, ())):
                names.add(f"{connection.net} ({connection.policy})")
    return sorted(names)


def _report_failure(
    field_: LayeredField, outcome: Negotiation, report: Report
) -> None:
    congested = field_.congested()
    cuts = sum(len(edges) for edges in congested.values())
    hint = (
        "the corridors these nets share have less room than the nets need; move the "
        "parts apart, widen the board, or add a signal layer"
    )
    if outcome.blocked_by:
        hint = (
            "held by "
            + ", ".join(outcome.blocked_by)
            + "; those nets were kept in place by their `rip_up` setting, so "
            "lowering it or moving the parts apart is what frees the corridor"
        )
    # A note, not a warning. "The negotiation did not settle" is a fact about the
    # search, not about the board that comes out of it: the realization pass may
    # still lay every one of these connections as legal copper, and often does. What
    # is worth a warning is a connection that could *not* be laid legally, and that
    # is reported where it happens, as a hand-over with the corridor that ran out of
    # room (ADR 0008).
    report.info(
        "routing-congested",
        f"congestion did not settle after {outcome.iterations} passes: {cuts} "
        f"corridor{'s' if cuts != 1 else ''} are subscribed for more copper than "
        "they have room for",
        hint=hint + ". Any connection that cannot then be realized legally is "
        "handed over rather than squeezed in",
    )

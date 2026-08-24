# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Taking back the vias and the copper the router spent and did not need.

M17a and M17b, and both are the same operation seen from two sides.

The M16 baseline measured 90 layer changes across the bundled corpus against a
lower bound of 14, and **37 of them were made by connections that never met a
corridor above half capacity** -- a via spent where congestion did not ask for one.
The same milestone's E2 guard, which was expected to find nothing, found a
connection on `examples/pcie-sata` that hopped to B.Cu for 0.55 mm, hopped straight
back, and retraced its own outbound path home: eight millimetres of copper and two
vias for nothing.

A connection that leaves a layer and comes back to it has a *span* -- legs *i*
through *j* with the same layer at both ends and vias in between. If that span
tightens as one leg on that layer, the vias in it were never needed. That is the
whole of this pass, and it answers both findings: M17a's unnecessary via and M17b's
retrace are the same span, accepted for two different reasons.

**It cannot make a board worse.** A span is collapsed only when it is strictly
better on both axes -- at least one via fewer, and not one micron more copper. A
rejected span leaves the connection exactly as the router built it, and the counts
of what was rejected and why are reported rather than swallowed.

**Who arbitrates.** Length is arithmetic. Legality is by construction: the
collapsed leg comes out of the funnel through free space that already excludes
every other net's copper, and :func:`aipcb.route.invariant.check_no_crossings` asks
the finished board afterwards. Congestion is M16a's cut model and *both* its
families -- the triangulation's own diagonals and the second diagonal of every
convex adjacent triangle pair. Soundness came first in M16a precisely so this pass
could lean on it: a via collapse that pushed a corridor past its width would be
trading a hole for a board that does not build.

**Who is excluded.** Controlled-impedance classes, and coupled pair legs. Removing
a via changes the geometry M11d's standoff rule and M11e's audit measured, and a
pair's two halves are offsets of one centre-line -- shortening either of them on
its own is exactly what breaks the relationship the pair exists for. They are
skipped by class rather than assumed safe, and a test asserts the skip.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aipcb.diagnostics import Report
from aipcb.netlist import Netlist
from aipcb.route.field import LayeredField
from aipcb.route.geometry import (
    class_for,
    connection_obstacles,
    geometry_for,
    path_length,
    rules_for,
    tighten_leg,
)
from aipcb.route.invariant import self_crossings
from aipcb.route.obstacles import Obstacle, RoutingEnvironment
from aipcb.route.stack import RoutingStack
from aipcb.route.stretch import (
    LayerGeometry,
    RoutedConnection,
    RouteRules,
    StretchError,
    StretchResult,
)
from aipcb.route.triangulate import FreeSpaceError

__all__ = ["Collapse", "TidyResult", "reclaim_vias"]

Point = tuple[float, float]

#: How much longer a collapsed span may be than the legs it replaces, in
#: millimetres. Zero, plus a rounding tolerance: this pass exists to take copper
#: back, never to spend it.
_LENGTH_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Collapse:
    """One span of a connection that was replaced by a single leg."""

    net: str
    connection: str
    layer: str
    vias_removed: int
    length_before: float
    length_after: float
    retrace: bool
    """Whether the span was laying copper twice -- the M16b E2 finding."""

    @property
    def saved(self) -> float:
        return self.length_before - self.length_after

    def describe(self) -> str:
        what = "a retrace, " if self.retrace else ""
        return (
            f"{self.net} {self.connection} gave back {what}"
            f"{self.vias_removed} via{'s' if self.vias_removed != 1 else ''} and "
            f"{self.saved:.3f} mm of copper on {self.layer}"
        )


@dataclass(slots=True)
class TidyResult:
    """What the pass took back, and what it looked at and left alone."""

    collapses: list[Collapse] = field(default_factory=list)
    considered: int = 0
    """Spans that were candidates -- a layer left and returned to."""
    rejected_length: int = 0
    rejected_capacity: int = 0
    rejected_geometry: int = 0
    skipped_controlled: int = 0
    """Connections left alone because their class is controlled-impedance."""

    @property
    def vias_removed(self) -> int:
        return sum(c.vias_removed for c in self.collapses)

    @property
    def copper_saved(self) -> float:
        return sum(c.saved for c in self.collapses)

    @property
    def retraces_removed(self) -> int:
        return sum(1 for c in self.collapses if c.retrace)

    def summary(self) -> dict[str, object]:
        return {
            "spans_considered": self.considered,
            "spans_collapsed": len(self.collapses),
            "vias_removed": self.vias_removed,
            "retraces_removed": self.retraces_removed,
            "copper_saved_mm": round(self.copper_saved, 3),
            "rejected": {
                "longer": self.rejected_length,
                "capacity": self.rejected_capacity,
                "unrealizable": self.rejected_geometry,
            },
            "skipped_controlled_impedance": self.skipped_controlled,
            "collapsed": [c.describe() for c in self.collapses],
        }


# ---------------------------------------------------------------------------
# the as-built cut occupancy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Occupancy:
    """How full every cut is, measured from the copper actually on the board.

    Not the negotiated ``used``: that is a symbolic subscription made before any
    geometry existed, and what this pass needs to know is whether the *built* board
    has room. Both cut families are charged, because M16a's whole finding is that
    the CDT's own diagonals are not the cut set (ADR 0014).
    """

    field_: LayeredField
    used: dict[str, list[float]] = field(default_factory=dict)
    special_used: dict[str, list[float]] = field(default_factory=dict)

    def charge(self, leg: StretchResult, demand: float, sign: float = 1.0) -> None:
        layer_field = self.field_.layers.get(leg.layer)
        if layer_field is None or len(leg.points) < 2:
            return
        for edge in layer_field.cuts_crossed(leg.points):
            self.used[leg.layer][edge] += sign * demand
        for cut in layer_field.special_cuts_crossed(leg.points):
            self.special_used[leg.layer][cut] += sign * demand

    def admits(
        self, removed: list[StretchResult], added: StretchResult, demand: float
    ) -> bool:
        """Would the swap leave a cut both fuller than it is and past its width?

        The test is deliberately *relative*. A corridor that is already
        over-subscribed on a built board is the shared, approximate field
        disagreeing with the exact per-net stretcher that laid the copper -- the
        M16 baseline reports 43 such cuts on a `pcie-sata` that is DRC-clean and
        crossing-free -- so demanding absolute headroom would refuse every collapse
        on precisely the boards that have vias to give back. What is refused is a
        swap that makes a cut worse *and* leaves it beyond what its width carries.
        """
        before = self._charges(removed, demand)
        after = self._charges([added], demand)
        for key in sorted(set(before) | set(after)):
            kind, layer, index = key
            layer_field = self.field_.layers[layer]
            if kind == "diagonal":
                now = self.used[layer][index]
                capacity = layer_field.capacity[index]
            else:
                now = self.special_used[layer][index]
                capacity = layer_field.special_capacity[index]
            settled = now - before.get(key, 0.0) + after.get(key, 0.0)
            if settled > capacity + 1e-9 and settled > now + 1e-9:
                return False
        return True

    def _charges(
        self, legs: list[StretchResult], demand: float
    ) -> dict[tuple[str, str, int], float]:
        """What ``legs`` would add to each cut, in millimetres of track-plus-clearance.

        The same unit as :attr:`used`, which is the whole point: the caller subtracts
        one of these from the built occupancy and adds the other, and a count of
        crossings would silently be comparing crossings against millimetres.
        """
        totals: dict[tuple[str, str, int], float] = {}
        for leg in legs:
            layer_field = self.field_.layers.get(leg.layer)
            if layer_field is None or len(leg.points) < 2:
                continue
            for edge in layer_field.cuts_crossed(leg.points):
                key = ("diagonal", leg.layer, edge)
                totals[key] = totals.get(key, 0.0) + demand
            for cut in layer_field.special_cuts_crossed(leg.points):
                key = ("special", leg.layer, cut)
                totals[key] = totals.get(key, 0.0) + demand
        return totals


def occupancy_of(
    field_: LayeredField, connections: list[RoutedConnection], netlist: Netlist
) -> Occupancy:
    """Charge every finished leg to both cut families, and hand back the totals."""
    occupancy = Occupancy(field_=field_)
    for layer, layer_field in field_.layers.items():
        if not layer_field.special:
            # Derived here rather than in `build_field`, which leaves them off
            # because charging them would change what the *router* thinks a
            # corridor costs (ADR 0014, Decision 2). This pass runs downstream of
            # every routing decision, so it can have the tighter cut set for free.
            layer_field.special = layer_field.triangulation.special_cuts()
            offset = field_.reference_clearance
            layer_field.special_capacity = [
                cut.length() + offset for cut in layer_field.special
            ]
            layer_field.special_used = [0.0] * len(layer_field.special)
        occupancy.used[layer] = [0.0] * len(layer_field.capacity)
        occupancy.special_used[layer] = [0.0] * len(layer_field.special)
    for connection in connections:
        rules = rules_for(netlist, connection.net)
        demand = rules.track_width + rules.clearance
        for leg in connection.legs:
            occupancy.charge(leg, demand)
    return occupancy


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


def reclaim_vias(
    connections: list[RoutedConnection],
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    field_: LayeredField,
    congestion: float,
    report: Report,
    allowed: tuple[str, ...] = (),
) -> TidyResult:
    """Collapse every span whose layer change the finished board did not need.

    Deterministic and order-stable: connections are visited in canonical order
    (net, then endpoints), spans left to right, and every decision is arithmetic
    over geometry that is already fixed. Idempotent, because a collapsed span
    leaves no span behind it: a second call over the same board finds nothing,
    which ``test_the_via_pass_is_idempotent`` asserts rather than assumes.
    """
    result = TidyResult()
    occupancy = occupancy_of(field_, connections, netlist)
    retracing = _retracing(connections)

    for connection in sorted(connections, key=_order):
        if not connection.vias or len(connection.legs) < 2:
            continue
        if class_for(netlist, connection.net).controlled_impedance or any(
            leg.coupled for leg in connection.legs
        ):
            result.skipped_controlled += 1
            continue
        _reclaim_one(
            connection,
            base,
            placed,
            netlist,
            stack,
            congestion,
            occupancy,
            retracing.get(_order(connection), frozenset()),
            result,
            allowed,
        )

    for collapse in result.collapses:
        report.info(
            "via-reclaimed",
            collapse.describe(),
            hint="the connection left the layer and came straight back to it; the "
            "corridor it was avoiding had room for it after all",
            net=collapse.net,
        )
    return result


def _order(connection: RoutedConnection) -> tuple[str, str, str]:
    return (connection.net, connection.start, connection.end)


def _retracing(
    connections: list[RoutedConnection],
) -> dict[tuple[str, str, str], frozenset[str]]:
    """Which connections lay copper twice, on which layer, before anything moves.

    The M16b E2 detector, used as an *input* rather than only as a guard: a span
    whose ends are on a layer this names is the retrace M17b was asked to
    eliminate, and the collapse is what eliminates it. Reading it here is also how
    the report can say which collapses were retraces and which were merely vias
    nobody needed.
    """
    by_connection: dict[tuple[str, str, str], set[str]] = {}
    index = {
        (c.net, f"{c.start}>{c.end}" if c.start else "route"): _order(c)
        for c in connections
    }
    for crossing in self_crossings(connections):
        if crossing.kind != "legs-meet":
            continue
        key = index.get((crossing.net, crossing.connection))
        if key is not None:
            by_connection.setdefault(key, set()).add(crossing.layer)
    return {key: frozenset(layers) for key, layers in by_connection.items()}


def _reclaim_one(
    connection: RoutedConnection,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    congestion: float,
    occupancy: Occupancy,
    retraced: frozenset[str],
    result: TidyResult,
    allowed: tuple[str, ...],
) -> None:
    """Try both shapes of unnecessary layer change on one connection.

    **The span.** Legs *i* to *j* with the same layer at both ends: the route left
    the layer and came back to it, so if it tightens as one leg the vias between
    were bought and not used. This is M17a's documented case and M17b's retrace.

    **The whole connection.** A route between two pads that both have copper on
    some layer never *had* to change layer at all -- the M16 baseline's own via
    lower bound is exactly the count of connections where that is false. So a
    connection whose two ends share a layer is asked, once per shared layer, whether
    it fits there in one leg.
    """
    context = _Context(
        connection=connection,
        base=base,
        placed=placed,
        netlist=netlist,
        stack=stack,
        congestion=congestion,
        occupancy=occupancy,
        retraced=retraced,
        result=result,
        rules=rules_for(netlist, connection.net, congestion),
        # The pads this connection was tightened against, not a guess at them. A
        # connection the repair pass rescued was allowed to land on every pad of
        # its own net, and re-tightening it against the strict set would be asking
        # whether a *different* route fits.
        open_pads=connection.open_pads
        or frozenset(
            name
            for name in (connection.start, connection.end)
            if name in base.pad_centres
        ),
    )

    start = 0
    while start < len(connection.legs) - 1:
        end = _span_end(connection, start)
        if end is None:
            start += 1
            continue
        result.considered += 1
        # `start` stays put when a collapse lands: the new leg may itself begin a
        # further span, and leaving the cursor here is what makes a second call
        # over the same board find nothing.
        if not context.collapse(start, end, connection.legs[start].layer):
            start += 1

    for layer in _shared_layers(connection, base, allowed):
        if not connection.vias:
            break
        result.considered += 1
        context.collapse(0, len(connection.legs) - 1, layer)


@dataclass(slots=True)
class _Context:
    """Everything one connection's collapses need, so the attempt reads as one call."""

    connection: RoutedConnection
    base: RoutingEnvironment
    placed: list[Obstacle]
    netlist: Netlist
    stack: RoutingStack
    congestion: float
    occupancy: Occupancy
    retraced: frozenset[str]
    result: TidyResult
    rules: RouteRules
    open_pads: frozenset[str]
    geometries: dict[str, LayerGeometry] = field(default_factory=dict)

    def collapse(self, start: int, end: int, layer: str) -> bool:
        """Replace legs ``start``..``end`` with one leg on ``layer``, if it pays."""
        connection = self.connection
        result = self.result
        demand = self.rules.track_width + self.rules.clearance
        removed = connection.legs[start : end + 1]
        if len(removed[0].points) < 2 or len(removed[-1].points) < 2:
            # A leg with no geometry has no end to tighten to. Nothing here builds
            # one, but a pattern generator may hand this pass copper it did not lay.
            result.rejected_geometry += 1
            return False
        before = sum(leg.length for leg in removed)

        geometry = self.geometries.get(layer)
        if geometry is None:
            try:
                geometry = geometry_for(
                    self.base,
                    self.placed,
                    self.netlist,
                    connection.net,
                    layer,
                    self.rules,
                    self.congestion,
                    open_pads=self.open_pads,
                )
            except (StretchError, FreeSpaceError):
                result.rejected_geometry += 1
                return False
            self.geometries[layer] = geometry
        try:
            points, crossings = tighten_leg(
                removed[0].points[0],
                removed[-1].points[-1],
                [],
                geometry,
                self.rules,
                f"route {connection.net}/{connection.start}>{connection.end}",
            )
        except (StretchError, FreeSpaceError):
            result.rejected_geometry += 1
            return False

        after = path_length(points)
        if after > before + _LENGTH_TOLERANCE:
            result.rejected_length += 1
            return False

        candidate = StretchResult(
            net=connection.net,
            layer=layer,
            points=points,
            width=self.rules.track_width,
            crossings=crossings,
            start=removed[0].start,
            end=removed[-1].end,
        )
        if not self.occupancy.admits(removed, candidate, demand):
            result.rejected_capacity += 1
            return False

        stale = {o.name for o in connection_obstacles(connection, self.stack)}
        for leg in removed:
            self.occupancy.charge(leg, demand, sign=-1.0)
        self.occupancy.charge(candidate, demand)
        for via in connection.vias[start:end]:
            connection.barrel_length -= self.stack.barrel_length(
                via.from_layer, via.to_layer
            )
        vias_removed = end - start
        connection.legs[start : end + 1] = [candidate]
        del connection.vias[start:end]
        # Its own copper never blocked it -- a net's tracks and vias are not
        # obstacles to the net that owns them -- but it blocks everything else, and
        # the next connection this pass looks at has to see the board as it stands.
        self.placed[:] = [o for o in self.placed if o.name not in stale]
        self.placed.extend(connection_obstacles(connection, self.stack))
        # The free space on every layer this connection touched has moved, so the
        # cached geometry is stale. Cheap to drop: a connection rarely has a second
        # span to give back.
        self.geometries.clear()
        result.collapses.append(
            Collapse(
                net=connection.net,
                connection=f"{connection.start}>{connection.end}",
                layer=layer,
                vias_removed=vias_removed,
                length_before=before,
                length_after=after,
                retrace=layer in self.retraced,
            )
        )
        return True


def _shared_layers(
    connection: RoutedConnection,
    base: RoutingEnvironment,
    allowed: tuple[str, ...],
) -> list[str]:
    """Routable layers both of this connection's ends can be reached on.

    Empty unless both ends are pads: a leg that starts at a via belongs to a
    pattern the source asked for -- a fanout escape, a pair transition -- and the
    hole at its end is not this pass's to take away. Empty too when the two pads
    share no layer, which is the M16 baseline's `via_lower_bound`: those connections
    have to change layer and no pass can take that away either.

    The layers the route already uses come first, because a route already running
    mostly on F.Cu is likelier to fit entirely on F.Cu than on a layer it has never
    touched, and trying the likely one first is what keeps this to one triangulation
    on the boards where it pays.
    """
    ends = (connection.start, connection.end)
    if any(not name or name not in base.pad_layers for name in ends):
        return []
    shared = set(allowed).intersection(
        *(_spans(base.pad_layers[name], allowed) for name in ends)
    )
    if not shared:
        return []
    used = list(dict.fromkeys(leg.layer for leg in connection.legs))
    return [layer for layer in used if layer in shared] + sorted(shared - set(used))


def _spans(layers: frozenset[str], allowed: tuple[str, ...]) -> set[str]:
    """A pad on ``*.Cu`` is on every layer, which is what a through-hole pad means."""
    return set(allowed) if "*.Cu" in layers else set(layers)


def _span_end(connection: RoutedConnection, start: int) -> int | None:
    """The last leg of the longest span that returns to ``start``'s layer."""
    layer = connection.legs[start].layer
    for end in range(len(connection.legs) - 1, start, -1):
        if connection.legs[end].layer == layer:
            return end
    return None

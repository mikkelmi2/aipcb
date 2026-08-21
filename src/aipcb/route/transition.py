"""The pair via-transition generator (M11c).

M8 refused to let a differential pair change layer, and the refusal was correct
for the reason ADR 0007 gives: a paired via column at the pair's own pitch is one
piece of copper, and splaying the halves out to via pitch and back is a distinct
pattern with its own discontinuity. This module is that pattern, built the way
M9e builds patterns -- deterministically, before anything is routed, with its
output published as ordinary copper, fixed obstacles and terminals.

What it lays, per declared transition:

* **two signal vias**, at matched geometry: the same size, the same span, and
  placed symmetrically about the point the source named, on the line
  perpendicular to the pair's direction of travel. Symmetric placement is the
  whole point -- a transition where one half vias a tenth of a millimetre before
  the other is a skew the meander cannot see;
* **ground return vias**, as many as the source asks for and as close as it
  allows, because the return current has to cross the layers with the signal. A
  position that fouls existing copper or another drill is skipped and counted
  rather than forced, so the report says two-of-two or one-of-two rather than
  claiming both;
* **two coupled pair segments**, one on each layer, which the router then routes
  as pairs in the ordinary way. The transition itself is never routed: it is a
  terminal on both layers at once, which is what a via is.

And it reports, per transition, the two numbers that decide whether the pattern is
any good: how many return vias it actually got, and how much stub the barrel
leaves below the layers the signal uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from aipcb.diagnostics import Report
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode
from aipcb.model.highspeed import PairTransition
from aipcb.model.layout import NetClass, Stackup, copper_layer_names
from aipcb.netlist import Netlist
from aipcb.route.diffpair import DiffPair, geometry_for_class
from aipcb.route.emit import HOLE_TO_HOLE_MM
from aipcb.route.geometry import rules_for, via_obstacles
from aipcb.route.obstacles import Obstacle, RoutingEnvironment, inflate
from aipcb.route.stack import RoutingStack
from aipcb.route.stretch import RoutedConnection, Via

__all__ = [
    "MAX_TRANSITION_VIAS",
    "TransitionEvent",
    "TransitionResult",
    "generate_transitions",
    "transition_uuid",
    "transition_uuids",
]

Point = tuple[float, float]

#: A cap on the vias one transition may own, so the UUID space it claims is a pure
#: function of the source. Two signal vias plus at most eight returns, with room to
#: spare; the same shape of bound as ``MAX_STITCH_VIAS``.
MAX_TRANSITION_VIAS = 16

#: How far a candidate return via must stay from copper of another net, on top of
#: the clearance that copper's own class asks for. Zero: the clearance check is
#: already the rule, and a second margin here would be a rule nobody wrote down.
_EXTRA_CLEARANCE = 0.0

#: How much larger the router's inflated hull is than the true offset it stands in
#: for. Obstacles are grown by the convex hull of circumscribed octagons, which
#: over-states a disc by ``1 / cos(pi/8)``; a generator that computes the room a
#: route needs has to allow for the same over-statement or it leaves the route a
#: corridor that is 8% too narrow. (:func:`aipcb.route.obstacles.inflate`.)
_INFLATION_OVERSHOOT = 1 / math.cos(math.pi / 8)


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    """What one transition produced, and the two numbers that judge it."""

    index: int
    label: str
    at: Point
    layers: tuple[str, str]
    pitch_mm: float
    """The pair's own centre-to-centre spacing, which the coupled run holds."""
    via_pitch_mm: float
    """What the two signal vias had to open out to, so their copper does not meet."""
    via_diameter_mm: float
    via_drill_mm: float
    return_asked: int
    return_placed: int
    return_within_mm: float
    return_distance_mm: float
    """The furthest a placed return via sits from the transition's centre."""
    stub_mm: float
    barrel: tuple[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition": self.index,
            "pair": self.label,
            "at": [round(self.at[0], 4), round(self.at[1], 4)],
            "layers": list(self.layers),
            "pitch_mm": round(self.pitch_mm, 4),
            "via_pitch_mm": round(self.via_pitch_mm, 4),
            "via": {"diameter": self.via_diameter_mm, "drill": self.via_drill_mm},
            "return_vias": {
                "asked": self.return_asked,
                "placed": self.return_placed,
                "within_mm": self.return_within_mm,
                "furthest_mm": round(self.return_distance_mm, 4),
            },
            "barrel": list(self.barrel),
            "stub_mm": round(self.stub_mm, 4),
        }


@dataclass(slots=True)
class TransitionResult:
    """What the generator produced, and how the router is to see it."""

    pairs: list[DiffPair] = field(default_factory=list)
    """The coupled segments the transitions split their pairs into."""
    handled: set[str] = field(default_factory=set)
    """Nets whose pairs this generator owns, so ``find_pairs`` leaves them alone."""
    connections: list[RoutedConnection] = field(default_factory=list)
    obstacles: list[Obstacle] = field(default_factory=list)
    terminals: dict[str, tuple[str, Point, frozenset[str]]] = field(
        default_factory=dict
    )
    events: list[TransitionEvent] = field(default_factory=list)

    def apply(self, environment: RoutingEnvironment) -> None:
        """Put the vias into the environment as copper and as terminals."""
        for obstacle in self.obstacles:
            environment.obstacles[obstacle.name] = obstacle
        for name, (net, point, layers) in self.terminals.items():
            environment.pad_centres[name] = point
            environment.pad_nets[name] = net
            environment.pad_layers[name] = layers

    def summary(self) -> dict[str, Any]:
        return {
            "transitions": len(self.events),
            "return_vias": sum(e.return_placed for e in self.events),
            "return_vias_asked": sum(e.return_asked for e in self.events),
            "worst_stub_mm": round(
                max((e.stub_mm for e in self.events), default=0.0), 4
            ),
            "events": [event.to_dict() for event in self.events],
        }


def transition_uuid(index: int, ordinal: int) -> str:
    """The stable identity of one via in one transition."""
    return element_uuid("transition", index, ordinal)


def transition_uuids(netlist: Netlist) -> set[str]:
    """Every UUID the declared transitions could produce.

    Enumerable from the source alone, like ``stitch_uuids``, which is what lets a
    second run recognise the previous one's copper without reading it.
    """
    return {
        transition_uuid(index, ordinal)
        for index in range(len(netlist.transitions))
        for ordinal in range(MAX_TRANSITION_VIAS)
    }


def generate_transitions(
    board: SNode,
    environment: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    report: Report,
    *,
    congestion: float = 1.0,
) -> TransitionResult:
    """Lay every declared pair via transition, before anything is routed."""
    del board
    result = TransitionResult()
    if not netlist.transitions:
        return result

    stackup = netlist.layout.stackup if netlist.layout is not None else Stackup()
    order = copper_layer_names(stackup.copper_layers)
    for index, intent in enumerate(netlist.transitions):
        _one(
            index, intent, environment, netlist, stack, stackup, order, congestion,
            result, report,
        )
    if result.events:
        placed = sum(e.return_placed for e in result.events)
        asked = sum(e.return_asked for e in result.events)
        report.info(
            "transition-generated",
            f"{len(result.events)} pair via transition"
            f"{'s' if len(result.events) != 1 else ''}, {placed} of {asked} return "
            f"via{'s' if asked != 1 else ''} placed; worst stub "
            f"{max(e.stub_mm for e in result.events):.3f} mm",
            hint="a transition is a terminal on two layers at once; the pair is "
            "routed as two coupled segments that meet at it",
        )
    return result


def _one(
    index: int,
    intent: PairTransition,
    environment: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    stackup: Stackup,
    order: tuple[str, ...],
    congestion: float,
    result: TransitionResult,
    report: Report,
) -> None:
    path: tuple[str | int, ...] = ("transitions", index)
    loc = netlist.locs.get(path)

    def refuse(code: str, message: str, hint: str) -> None:
        report.error(code, message, loc=loc, path=path, hint=hint)

    positive, negative = sorted(intent.pair)
    nets = {name: netlist.nets.get(name) for name in (positive, negative)}
    if any(net is None for net in nets.values()):
        refuse(
            "transition-unknown-net",
            f"transition {index} names {intent.pair}, and this design has no such net",
            "a transition names the two nets of a pair the design already declares",
        )
        return
    partners = {
        name: (net.attrs.diff_pair if net is not None else None)
        for name, net in nets.items()
    }
    if partners[positive] != negative or partners[negative] != positive:
        refuse(
            "transition-not-a-pair",
            f"transition {index} names {positive} and {negative}, which do not "
            "declare each other with `diff_pair:`",
            "a via transition is a pattern for a pair; two nets that are not a pair "
            "each get an ordinary via",
        )
        return

    for layer in intent.between:
        if layer not in order:
            refuse(
                "transition-unknown-layer",
                f"transition {index} crosses to {layer}, which this "
                f"{stackup.copper_layers}-layer board does not have",
                f"copper layers on this board: {', '.join(order)}",
            )
            return

    elaborated = nets[positive]
    assert elaborated is not None
    class_name = elaborated.net_class
    net_class = netlist.net_classes.get(class_name, NetClass())
    width, gap = geometry_for_class(
        netlist, class_name, net_class, elaborated, report, positive
    )
    pitch = width + gap

    rules = rules_for(netlist, positive, congestion)
    diameter = intent.via.diameter if intent.via else rules.via_diameter
    drill = intent.via.drill if intent.via else rules.via_drill

    # The two signal vias cannot sit at the pair's own pitch. ADR 0007 refused to
    # build a paired via column for exactly this reason -- "a 0.6 mm via at a
    # 0.54 mm pitch is one piece of copper" -- and it is arithmetic, not taste: two
    # 0.4 mm vias 0.439 mm apart leave 0.039 mm of laminate between two nets that
    # want 0.15 mm. So the column opens out to whatever the vias and the class
    # clearance need, the halves splay to reach it, and the splay is uncoupled
    # length that M11d's budget then counts. That splay *is* the discontinuity
    # ADR 0007 named; building it deliberately and measuring it is the difference
    # between this and pretending it is not there.
    via_pitch = max(pitch, diameter + net_class.clearance_mm)

    frame_at = _to_board(netlist, intent.at)
    ends = _pads_by_layer(environment, (positive, negative), intent.between)
    if ends is None:
        refuse(
            "transition-pads-not-found",
            f"transition {index} on {positive}+{negative}: each net needs one pad "
            f"reachable on {intent.between[0]} and one on {intent.between[1]}",
            "a transition joins the part of a pair on one layer to the part on the "
            "other; without a pad on each layer there is nothing for it to join",
        )
        return

    axis = _axis(environment, ends, frame_at)
    across = (-axis[1], axis[0])
    signal_at = {
        positive: _offset(frame_at, across, -via_pitch / 2),
        negative: _offset(frame_at, across, via_pitch / 2),
    }
    # Keep the pair the same way round it already is: whichever half is on the
    # negative side of the travel axis at the pads stays on it through the via.
    if _side(environment, ends[positive][0], frame_at, axis) > 0:
        signal_at = {
            positive: _offset(frame_at, across, via_pitch / 2),
            negative: _offset(frame_at, across, -via_pitch / 2),
        }

    ordinal = 0
    for net in (positive, negative):
        via = Via(
            net=net,
            point=signal_at[net],
            from_layer=intent.between[0],
            to_layer=intent.between[1],
            diameter=diameter,
            drill=drill,
            kind=stack.via_type(*intent.between) or "through",
            name=f"transition:{index}#{ordinal}",
        )
        ordinal += 1
        result.connections.append(
            RoutedConnection(
                net=net,
                start=f"{net}@tr{index}",
                end=f"{net}@tr{index}",
                vias=[via],
                barrel_length=stack.barrel_length(*intent.between),
            )
        )
        result.obstacles.extend(
            via_obstacles(via, stack, f"transition:{index}/{net}")
        )
        result.terminals[f"{net}@tr{index}"] = (
            net,
            via.point,
            frozenset(intent.between),
        )
        # Republished as a pad-shaped obstacle so the router can land on it, the
        # same trick the fanout plays with its outermost escape via.
        result.obstacles.append(
            Obstacle(
                name=f"{net}@tr{index}",
                polygon=_disc(via.point, diameter / 2),
                net=net,
                layers=frozenset(),
                kind="pad",
            )
        )

    # How far out of the pair's own way a return via has to sit. The pair arrives
    # along the axis and leaves along it on the other layer, so the only room for a
    # return via is across -- and it has to be far enough across that the pair's
    # corridor, standoff included, still fits between the two of them. Placing them
    # by "as close as looks right" instead was measured: 0.925 mm on this geometry
    # against the 1.007 mm the corridor needs, and the pair was refused.
    keep = (
        (2 * width + gap) / 2
        + net_class.standoff * net_class.clearance_mm
        + diameter / 2
        + net_class.clearance_mm
    ) * _INFLATION_OVERSHOOT
    placed, furthest = _return_vias(
        index, intent, environment, netlist, stack, congestion,
        frame_at, across, via_pitch, diameter, drill, keep, ordinal, result,
    )

    span = stack.barrel_span(*intent.between)
    barrel = (span[0], span[-1]) if span else intent.between
    shallow, deep = sorted(intent.between, key=order.index)
    stub = stackup.barrel_length_mm(barrel[0], shallow) + stackup.barrel_length_mm(
        deep, barrel[1]
    )

    result.events.append(
        TransitionEvent(
            index=index,
            label=f"{positive}+{negative}",
            at=frame_at,
            layers=intent.between,
            pitch_mm=pitch,
            via_pitch_mm=via_pitch,
            via_diameter_mm=diameter,
            via_drill_mm=drill,
            return_asked=intent.return_vias,
            return_placed=placed,
            return_within_mm=intent.return_within_mm,
            return_distance_mm=furthest,
            stub_mm=stub,
            barrel=barrel,
        )
    )
    if placed < intent.return_vias:
        report.warning(
            "transition-return-vias",
            f"transition {index} on {positive}+{negative} placed {placed} of "
            f"{intent.return_vias} return vias within "
            f"{intent.return_within_mm} mm; the rest had nowhere legal to go",
            loc=loc,
            path=path,
            hint="the return current crosses layers with the signal or it goes the "
            "long way round; make room beside the transition, or widen "
            "`return_within_mm` and accept the larger loop",
        )

    result.handled.update((positive, negative))
    for segment, (layer, starts, ends_) in enumerate(
        (
            (intent.between[0], (ends[positive][0], ends[negative][0]),
             (f"{positive}@tr{index}", f"{negative}@tr{index}")),
            (intent.between[1], (f"{positive}@tr{index}", f"{negative}@tr{index}"),
             (ends[positive][1], ends[negative][1])),
        )
    ):
        result.pairs.append(
            DiffPair(
                positive=positive,
                negative=negative,
                starts=starts,
                ends=ends_,
                width=width,
                gap=gap,
                max_skew=net_class.max_skew_mm,
                net_class=class_name,
                max_uncoupled=net_class.max_uncoupled_mm,
                standoff=net_class.standoff,
                target_ohm=net_class.impedance_diff_ohm,
                layer=layer,
                segment=f"tr{index}.{segment}",
            )
        )


def _return_vias(
    index: int,
    intent: PairTransition,
    environment: RoutingEnvironment,
    netlist: Netlist,
    stack: RoutingStack,
    congestion: float,
    at: Point,
    across: Point,
    pitch: float,
    diameter: float,
    drill: float,
    keep: float,
    ordinal: int,
    result: TransitionResult,
) -> tuple[int, float]:
    """Ground vias beside the pair, as many as asked and as close as allowed.

    They go on the line the two signal vias sit on and nowhere else. That line is
    the only direction the pair does not occupy: it arrives along the travel axis on
    one layer and leaves along it on the other, so a return via anywhere else is a
    via in the pair's way. Sides alternate, and each further pair steps out by one
    via plus a clearance.
    """
    if intent.return_vias <= 0:
        return (0, 0.0)
    rules = rules_for(netlist, intent.return_net, congestion)
    step_out = diameter + max(rules.clearance, HOLE_TO_HOLE_MM)
    inner = max(
        keep, pitch / 2 + diameter + max(rules.clearance, HOLE_TO_HOLE_MM)
    )
    placed = 0
    furthest = 0.0
    for step in range(intent.return_vias):
        radius = inner + (step // 2) * step_out
        if radius > intent.return_within_mm:
            continue
        direction = 1 if step % 2 == 0 else -1
        offset = (direction * across[0], direction * across[1])
        point = (
            round(at[0] + offset[0] * radius, 6),
            round(at[1] + offset[1] * radius, 6),
        )
        if not _is_clear(point, diameter, drill, environment, netlist, congestion,
                         intent.return_net, result):
            continue
        via = Via(
            net=intent.return_net,
            point=point,
            from_layer=intent.between[0],
            to_layer=intent.between[1],
            diameter=diameter,
            drill=drill,
            kind=stack.via_type(*intent.between) or "through",
            name=f"transition:{index}#{ordinal + step}",
        )
        result.connections.append(
            RoutedConnection(
                net=intent.return_net,
                start=f"{intent.return_net}@tr{index}.{step}",
                end=f"{intent.return_net}@tr{index}.{step}",
                vias=[via],
                barrel_length=stack.barrel_length(*intent.between),
            )
        )
        result.obstacles.extend(
            via_obstacles(via, stack, f"transition:{index}/return{step}")
        )
        placed += 1
        furthest = max(furthest, math.dist(at, point))
    return (placed, furthest)


def _is_clear(
    point: Point,
    diameter: float,
    drill: float,
    environment: RoutingEnvironment,
    netlist: Netlist,
    congestion: float,
    net: str,
    result: TransitionResult,
) -> bool:
    """Copper clearance to everything else, and drill web to every other hole."""
    from shapely.geometry import Point as ShapelyPoint
    from shapely.geometry import Polygon as ShapelyPolygon

    here = ShapelyPoint(point).buffer(diameter / 2)
    for obstacle in (*environment.obstacles.values(), *result.obstacles):
        if len(obstacle.polygon) < 3:
            continue
        margin = (
            0.0
            if obstacle.net == net
            else rules_for(netlist, obstacle.net or net, congestion).clearance
            + _EXTRA_CLEARANCE
        )
        shape = ShapelyPolygon(inflate(obstacle.polygon, margin))
        if shape.intersects(here):
            return False
    for connection in result.connections:
        for other in connection.vias:
            if math.dist(other.point, point) < (drill + other.drill) / 2 + HOLE_TO_HOLE_MM:
                return False
    return True


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _to_board(netlist: Netlist, point: Point) -> Point:
    from aipcb.compile.frame import frame_for

    frame = frame_for(netlist)
    if frame is None:
        origin = netlist.layout.origin_mm if netlist.layout else (100.0, 100.0)
        return (origin[0] + point[0], origin[1] + point[1])
    return frame.to_kicad(point)


def _pads_by_layer(
    environment: RoutingEnvironment,
    nets: tuple[str, str],
    layers: tuple[str, str],
) -> dict[str, tuple[str, str]] | None:
    """One pad per net per layer: ``{net: (pad on layers[0], pad on layers[1])}``."""
    out: dict[str, tuple[str, str]] = {}
    for net in nets:
        pads = sorted(
            key for key, pad_net in environment.pad_nets.items() if pad_net == net
        )
        chosen: list[str] = []
        for layer in layers:
            here = [
                pad
                for pad in pads
                if not environment.pad_layers.get(pad, frozenset())
                or layer in environment.pad_layers.get(pad, frozenset())
                or "*.Cu" in environment.pad_layers.get(pad, frozenset())
            ]
            if not here:
                return None
            chosen.append(here[0] if len(here) == 1 else _pick(here, chosen))
        if chosen[0] == chosen[1]:
            return None
        out[net] = (chosen[0], chosen[1])
    return out


def _pick(candidates: list[str], taken: list[str]) -> str:
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    return candidates[0]


def _axis(
    environment: RoutingEnvironment, ends: dict[str, tuple[str, str]], at: Point
) -> Point:
    """The pair's direction of travel through the transition, as a unit vector."""
    first = _centroid(
        environment, [pads[0] for pads in ends.values()]
    )
    second = _centroid(
        environment, [pads[1] for pads in ends.values()]
    )
    if first is None or second is None:
        return (0.0, 1.0)
    dx, dy = second[0] - first[0], second[1] - first[1]
    span = math.hypot(dx, dy)
    if span <= 0:
        return (0.0, 1.0)
    del at
    return (dx / span, dy / span)


def _side(
    environment: RoutingEnvironment, pad: str, at: Point, axis: Point
) -> float:
    """Which side of the travel axis a pad sits on. Positive is left."""
    centre = environment.pad_centres.get(environment.resolve_pad(pad) or pad)
    if centre is None:
        return 0.0
    return axis[0] * (centre[1] - at[1]) - axis[1] * (centre[0] - at[0])


def _centroid(
    environment: RoutingEnvironment, pads: list[str]
) -> Point | None:
    points = [
        environment.pad_centres.get(environment.resolve_pad(pad) or pad)
        for pad in pads
    ]
    known = [p for p in points if p is not None]
    if not known:
        return None
    return (
        sum(p[0] for p in known) / len(known),
        sum(p[1] for p in known) / len(known),
    )


def _offset(point: Point, direction: Point, distance: float) -> Point:
    return (
        round(point[0] + direction[0] * distance, 6),
        round(point[1] + direction[1] * distance, 6),
    )


def _disc(centre: Point, radius: float, segments: int = 12) -> tuple[Point, ...]:
    scale = radius / math.cos(math.pi / segments)
    return tuple(
        (
            round(centre[0] + scale * math.cos(math.tau * i / segments), 6),
            round(centre[1] + scale * math.sin(math.tau * i / segments), 6),
        )
        for i in range(segments)
    )

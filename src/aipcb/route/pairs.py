"""Differential pairs: routed as one thing, realized as two runs with a neck.

A pair is not two nets that happen to run alongside each other -- its impedance
comes from the coupling between them -- so it is routed as a single object with a
single path, and only becomes two pieces of copper at the last moment. The centre
line is tightened once, as though it were one fat track wide enough for both traces
and the gap, and then offset to either side; the gap is correct everywhere by
construction, corners included.

What is *not* by construction is either end. Where the two halves leave the pair's
own pitch for pads that are somewhere else, they are on their own, and this module
is mostly about being honest about that:

* the fan-out is tightened rather than drawn, so it goes round what is in its way;
* it necks down to the class's ordinary trace width, because it is not coupled to
  anything and because a pair trace often cannot land on a fine-pitch pad at all;
* the coupled run is trimmed to the part of it that is genuinely clear;
* and every check that could refuse the pair is a measurement, reported with its
  number, rather than a shrug.

Where the answer is "this is not a pair on this board", it says so and the caller
routes the halves separately -- which is worse, and true.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any

from aipcb.diagnostics import Report
from aipcb.netlist import Netlist
from aipcb.route.diffpair import DiffPair, split_centre_line
from aipcb.route.geometry import (
    geometry_for,
    resample,
    rules_for,
    simplify,
    tighten_leg,
    track_obstacles,
)
from aipcb.route.graph import RoutePath
from aipcb.route.meander import lengthen
from aipcb.route.negotiate import Connection
from aipcb.route.obstacles import Obstacle, RoutingEnvironment
from aipcb.route.stack import RoutingStack
from aipcb.route.stretch import (
    LayerGeometry,
    RoutedConnection,
    RouteRules,
    StretchError,
    StretchResult,
    stretch_guided,
)
from aipcb.route.triangulate import FreeSpaceError

__all__ = [
    "MAX_UNCOUPLED",
    "PairAudit",
    "WallHug",
    "measure_skew",
    "realize_pair",
]

Point = tuple[float, float]

#: M11d rule 2, the two numbers. A controlled-impedance segment that runs within
#: ``HUG_DISTANCE x gap`` of another copper feature for more than
#: ``HUG_LENGTH x gap`` is hugging a wall: close enough for that feature to be part
#: of the field the pair sees, and for long enough that it matters. Both are
#: multiples of the pair's own gap because the gap is the length the coupling works
#: at -- a feature three gaps away is most of a decade down in influence, and a
#: run of five gaps is where the discontinuity stops being a corner and starts
#: being a section of transmission line with the wrong impedance.
HUG_DISTANCE = 3.0
HUG_LENGTH = 5.0

#: How much of a pair may be fan-out before it stops being worth calling coupled.
#: A short uncoupled breakout at each end is normal and unavoidable -- pads are
#: never at the pair's own pitch -- so this is a judgement call rather than a
#: physical limit, and the actual figure is reported either way.
#:
#: A third, rather than M7's quarter. The fan-out is now *tightened* rather than
#: drawn straight at the pads, so it goes round what is in its way and is honestly
#: longer for it -- on `usb-port` the device-side pair measures 28%, where the
#: straight line that used to be drawn measured less and went through a pad.
MAX_UNCOUPLED = 1 / 3

#: How far inside an inflated hull a path has to go before it counts as a collision
#: rather than as a tightened path wrapped around it, in millimetres. A nanometre --
#: KiCad's own resolution, so anything this close is the same point.
_GRAZE = 1e-6

#: How finely the coupled run is sampled when looking for the part of it that is
#: genuinely clear, in millimetres.
_TRIM_STEP = 0.05

#: How finely rule 2 samples a stretch that passes close to something, in
#: millimetres. Only the part already inside the skirt is sampled, so this is a
#: few dozen points per feature rather than a few hundred per board.
_HUG_STEP = 0.05


@dataclass(frozen=True, slots=True)
class WallHug:
    """A stretch of coupled run that spent too long too close to something else."""

    feature: str
    """The obstacle's key -- ``J1.6#7`` for a pad, ``track:GND/...`` for copper."""
    net: str | None
    layer: str
    length_mm: float
    """How far the pair ran within :data:`HUG_DISTANCE` gaps of it."""
    closest_mm: float
    """The nearest the two came, copper edge to copper edge."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "net": self.net,
            "layer": self.layer,
            "length_mm": round(self.length_mm, 4),
            "closest_mm": round(self.closest_mm, 4),
        }


@dataclass(frozen=True, slots=True)
class PairAudit:
    """What M11d measured about one pair. The M11e report's routing-side input."""

    key: str
    positive: str
    negative: str
    net_class: str
    layer: str
    coupled: bool
    reason: str | None
    """Why it is not coupled, when it is not."""
    width_mm: float
    gap_mm: float
    standoff: float
    target_ohm: float | None
    uncoupled_mm: tuple[float, ...] = ()
    """Uncoupled length per half, in source order: positive first."""
    budget_mm: float | None = None
    wall_hugs: tuple[WallHug, ...] = ()
    retightened: bool = False
    """Whether rule 2's one re-tighten was tried."""
    resolved_by_retighten: bool = False

    @property
    def worst_uncoupled(self) -> float:
        return max(self.uncoupled_mm) if self.uncoupled_mm else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.key,
            "net_class": self.net_class,
            "layer": self.layer,
            "coupled": self.coupled,
            "reason": self.reason,
            "width_mm": self.width_mm,
            "gap_mm": self.gap_mm,
            "standoff": self.standoff,
            "target_ohm": self.target_ohm,
            "uncoupled_mm": [round(v, 4) for v in self.uncoupled_mm],
            "max_uncoupled_mm": self.budget_mm,
            "wall_hugs": [hug.to_dict() for hug in self.wall_hugs],
            "retightened": self.retightened,
            "resolved_by_retighten": self.resolved_by_retighten,
        }


def measure_skew(
    pairs: list[DiffPair],
    connections: list[RoutedConnection],
    report: Report,
) -> dict[str, float]:
    """How far out of length each routed pair ended up, reported against its budget.

    Length here is the whole conductor, via barrels included. Meandering has already
    had its chance by the time this runs; what is left is what could not be closed,
    and saying so is the point.
    """
    skew: dict[str, float] = {}
    for pair in pairs:
        if pair.key() in skew:
            # A pair split by a via transition arrives here once per segment. The
            # skew that matters is the whole conductor's, which the sum below
            # already is, so the second segment has nothing left to say.
            continue
        halves: dict[str, float] = {}
        for connection in connections:
            if connection.net in (pair.positive, pair.negative):
                halves[connection.net] = halves.get(connection.net, 0.0) + connection.length
        if len(halves) != 2:
            continue
        first, second = (halves[net] for net in (pair.positive, pair.negative))
        out_of_length = abs(first - second)
        skew[pair.key()] = out_of_length
        if pair.max_skew is not None and out_of_length > pair.max_skew:
            report.warning(
                "diff-pair-skew",
                f"{pair.key()} is {out_of_length:.3f} mm out of length, against a "
                f"{pair.max_skew:.3f} mm budget",
                hint="the mismatch comes from the outside of each bend being longer; "
                "shorten the run, straighten it, or raise `max_skew_mm`",
                net=pair.positive,
            )
    return skew


def realize_pair(
    connection: Connection,
    path: RoutePath,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    stack: RoutingStack,
    congestion: float,
    report: Report,
    audits: list[PairAudit] | None = None,
) -> list[RoutedConnection] | None:
    """Tighten a pair's centre-line, then offset it into two traces.

    The centre-line is tightened as though it were one fat track: both traces and
    the gap between them. Whatever clears that corridor clears the pair, and the gap
    is then correct everywhere by construction, corners included.

    Returns ``None`` when the result would not be a pair worth calling one -- which
    is a refusal, not a failure, and the caller falls back to routing the halves
    separately after saying why.

    **M11d.** A pair whose class names an ``impedance_diff_ohm`` gets three extra
    rules, and only such a pair does -- everything else takes exactly the path it
    took before:

    1. the centre-line is tightened against ``clearance x standoff`` rather than
       the bare minimum, so ordinary copper stops crowding the pair's field;
    2. a coupled run that spends more than :data:`HUG_LENGTH` gaps within
       :data:`HUG_DISTANCE` gaps of another feature is re-tightened once with that
       feature's clearance inflated, and if that does not resolve it the flag
       stands and reaches the M11e report;
    3. ``max_uncoupled_mm`` is a hard budget. Over it, the pair is *refused* --
       raised rather than returned, so the caller hands it over instead of quietly
       routing the halves separately.
    """
    pair = connection.pair
    assert isinstance(pair, DiffPair)
    controlled = pair.target_ohm is not None

    first = Report()
    results = _attempt(pair, path, base, placed, netlist, congestion, first, None)

    hugs: tuple[WallHug, ...] = ()
    retightened = False
    resolved = False
    if results is not None and controlled:
        hugs = _wall_hugging(pair, results, base, placed, path.legs[0].layer)
        if hugs:
            retightened = True
            floors = {
                hug.net: HUG_DISTANCE * pair.gap for hug in hugs if hug.net is not None
            }
            second = Report()
            again = _attempt(
                pair, path, base, placed, netlist, congestion, second, floors
            )
            if again is not None:
                once_more = _wall_hugging(
                    pair, again, base, placed, path.legs[0].layer
                )
                if len(once_more) < len(hugs):
                    results, first, hugs = again, second, once_more
                    resolved = not once_more

    report.extend(first.diagnostics)

    if results is None:
        _record(
            audits, pair, path, coupled=False, results=None,
            reason=_refusal_reason(first),
            hugs=hugs, retightened=retightened, resolved=resolved,
        )
        return None

    uncoupled = _uncoupled_lengths(results, pair)
    if pair.max_uncoupled is not None and max(uncoupled) > pair.max_uncoupled:
        detail = ", ".join(
            f"{half.net} {length:.3f} mm" for half, length in zip(results, uncoupled, strict=True)
        )
        _record(
            audits, pair, path, coupled=False, results=results,
            reason="uncoupled budget exceeded",
            hugs=hugs, retightened=retightened, resolved=resolved,
        )
        raise StretchError(
            f"{pair.key()} runs {max(uncoupled):.3f} mm uncoupled against a "
            f"{pair.max_uncoupled:.3f} mm budget ({detail}); `coupling` on class "
            f"{pair.net_class!r} makes that a refusal rather than a warning",
            hint="the uncoupled length is the fan-out at the ends plus anything the "
            "pair goes through in the middle; move the end components closer to the "
            "pair's own pitch, or raise `max_uncoupled_mm` and accept the "
            "discontinuity",
        )

    if hugs:
        listing = "; ".join(
            f"{hug.feature} at {hug.closest_mm:.3f} mm for {hug.length_mm:.3f} mm"
            for hug in hugs
        )
        report.warning(
            "diff-pair-wall-hugging",
            f"{pair.key()} runs parallel to {len(hugs)} copper feature"
            f"{'s' if len(hugs) != 1 else ''} closer than {HUG_DISTANCE:g} x its "
            f"{pair.gap} mm gap and for longer than {HUG_LENGTH:g} x it: {listing}"
            + (
                ". Re-tightening with their clearance inflated did not move it"
                if retightened
                else ""
            ),
            hint="the impedance of a coupled pair depends on what is beside it as "
            "well as what is under it; move the feature, give the pair a wider "
            "`standoff_k`, or accept the deviation with the number in front of you",
            net=pair.positive,
        )

    _record(
        audits, pair, path, coupled=True, results=results, reason=None,
        hugs=hugs, retightened=retightened, resolved=resolved,
    )
    return results


def _attempt(
    pair: DiffPair,
    path: RoutePath,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    congestion: float,
    report: Report,
    clearance_floor: dict[str, float] | None,
) -> list[RoutedConnection] | None:
    """One go at realizing the pair, with an optional per-net clearance floor."""
    both = frozenset({pair.positive, pair.negative})
    base_rules = rules_for(netlist, pair.positive, congestion)
    # M11d rule 1. The corridor the centre-line is tightened in stands off by
    # `standoff` times the class clearance rather than by the class clearance --
    # `standoff` is 1.0 for every class that does not ask for a controlled
    # impedance, which is why nothing else on any board moves. Applied to the
    # *centre-line* only: the fan-out at each end is not coupled to anything, and
    # asking it to stand off as well is what makes a pair leaving a 0.5 mm pitch
    # untightenable.
    centre_rules = replace(
        base_rules,
        track_width=pair.corridor,
        clearance=base_rules.clearance * pair.standoff,
    )

    starts = [base.pad_centres.get(base.resolve_pad(p) or "") for p in pair.starts]
    ends = [base.pad_centres.get(base.resolve_pad(p) or "") for p in pair.ends]
    if any(p is None for p in (*starts, *ends)):
        return None

    centre_start = _midpoint(starts)  # type: ignore[arg-type]
    centre_end = _midpoint(ends)  # type: ignore[arg-type]

    centre: list[Point] = []
    for index, leg in enumerate(path.legs):
        geometry = geometry_for(
            base,
            placed,
            netlist,
            both,
            leg.layer,
            centre_rules,
            congestion,
            open_pads=frozenset(
                key
                for key in (
                    base.resolve_pad(p) for p in (*pair.starts, *pair.ends)
                )
                if key
            ),
            clearance_floor=clearance_floor,
        )
        start = centre_start if index == 0 else leg.start
        end = centre_end if index == len(path.legs) - 1 else leg.end
        try:
            points, _ = tighten_leg(
                start,
                end,
                list(leg.guides),
                geometry,
                centre_rules,
                f"pair {pair.key()}",
            )
        except (StretchError, FreeSpaceError) as exc:
            standoff = (
                f" The corridor it was tightened in stands off "
                f"{pair.standoff:g} x the class's {base_rules.clearance:g} mm "
                f"clearance (M11d rule 1), which is very likely what refused it: "
                f"lower `standoff_k` on class {pair.net_class!r} if the package "
                "this pair leaves cannot give that much room."
                if pair.standoff > 1
                else ""
            )
            report.warning(
                "diff-pair-not-coupled",
                f"could not route {pair.key()} as a coupled pair: "
                f"{getattr(exc, 'message', exc)}",
                hint="the two halves will be routed separately, so the gap and the "
                "skew budget are no longer guaranteed." + standoff,
                net=pair.positive,
            )
            return None
        centre.extend(points if not centre else points[1:])

    if len(path.legs) > 1:
        report.info(
            "diff-pair-via",
            f"{pair.key()} changes layer {len(path.vias)} time"
            f"{'s' if len(path.vias) != 1 else ''}; both halves via together, so the "
            "gap is maintained through the transition",
            net=pair.positive,
        )

    return _split_pair(
        pair, centre, path, base, placed, netlist, congestion, report
    )


def _refusal_reason(report: Report) -> str | None:
    """The message of the refusal that ended an attempt, for the audit."""
    for diagnostic in reversed(report.diagnostics):
        if diagnostic.code == "diff-pair-not-coupled":
            return diagnostic.message
    return None


def _uncoupled_lengths(
    results: list[RoutedConnection], pair: DiffPair
) -> tuple[float, ...]:
    """How much of each half is not coupled to the other, in millimetres.

    The coupled run carries the pair's own width and the fan-out does not, so the
    legs say which is which without anything having to remember. Measured *after*
    length matching, because a meander is added to a fan-out leg and a budget
    checked before it is a budget checked against the wrong number.
    """
    return tuple(
        sum(leg.length for leg in half.legs if leg.width != pair.width)
        for half in results
    )


def _record(
    audits: list[PairAudit] | None,
    pair: DiffPair,
    path: RoutePath,
    *,
    coupled: bool,
    results: list[RoutedConnection] | None,
    reason: str | None,
    hugs: tuple[WallHug, ...],
    retightened: bool,
    resolved: bool,
) -> None:
    if audits is None:
        return
    audits.append(
        PairAudit(
            key=pair.key(),
            positive=pair.positive,
            negative=pair.negative,
            net_class=pair.net_class,
            layer=path.legs[0].layer if path.legs else "",
            coupled=coupled,
            reason=reason,
            width_mm=pair.width,
            gap_mm=pair.gap,
            standoff=pair.standoff,
            target_ohm=pair.target_ohm,
            uncoupled_mm=_uncoupled_lengths(results, pair) if results else (),
            budget_mm=pair.max_uncoupled,
            wall_hugs=hugs,
            retightened=retightened,
            resolved_by_retighten=resolved,
        )
    )


def _wall_hugging(
    pair: DiffPair,
    results: list[RoutedConnection],
    base: RoutingEnvironment,
    placed: list[Obstacle],
    layer: str,
) -> tuple[WallHug, ...]:
    """M11d rule 2: coupled run beside another copper feature, for too long.

    Measured against *physical* copper, never against an inflated hull. A correctly
    tightened path runs along an inflated hull for millimetres by construction --
    that is what tightening is -- so measuring against the hull would flag every
    pair on every board. The question here is a different one: how close is the
    real copper, and for how far.
    """
    from shapely.geometry import LineString
    from shapely.geometry import Polygon as ShapelyPolygon

    near = HUG_DISTANCE * pair.gap
    limit = HUG_LENGTH * pair.gap
    if near <= 0 or limit <= 0:
        return ()
    mine = {pair.positive, pair.negative}

    runs: list[LineString] = []
    for half in results:
        for leg in half.legs:
            if leg.width == pair.width and len(leg.points) >= 2:
                runs.append(LineString(leg.points))
    if not runs:
        return ()
    span = _bounds(runs)

    hugs: list[WallHug] = []
    for obstacle in sorted(
        {o.name: o for o in (*base.obstacles.values(), *placed)}.items()
    ):
        name, feature = obstacle
        if feature.net in mine or feature.kind == "body":
            continue
        if feature.layers and layer not in feature.layers and "*.Cu" not in feature.layers:
            continue
        if len(feature.polygon) < 3 or not _overlaps(span, feature.polygon, near):
            continue
        shape = ShapelyPolygon(feature.polygon)
        if not shape.is_valid:
            shape = shape.buffer(0)
        skirt = shape.buffer(near + pair.width / 2)
        length = 0.0
        closest = float("inf")
        for run in runs:
            for stretch, nearest in _parallel_stretches(run, shape, skirt, pair):
                length = max(length, stretch)
                closest = min(closest, nearest)
        if length > limit:
            hugs.append(
                WallHug(
                    feature=name,
                    net=feature.net,
                    layer=layer,
                    length_mm=length,
                    closest_mm=max(closest, 0.0),
                )
            )
    return tuple(sorted(hugs, key=lambda h: (-h.length_mm, h.feature)))


def _parallel_stretches(
    run: Any, shape: Any, skirt: Any, pair: DiffPair
) -> list[tuple[float, float]]:
    """The stretches of ``run`` that keep a roughly constant, too-small distance.

    "Runs parallel to" is the load-bearing word in M11d rule 2, and it is what
    separates a pair that hugs a wall from one that merely goes past the end of a
    pad. A path crossing a feature sweeps in and out; a path running beside it
    holds its distance. So a stretch counts only while the distance stays within
    one gap of the closest approach in that stretch -- which on a fine-pitch
    package escape measures the pad's own length and nothing else.
    """
    from shapely.geometry import Point as ShapelyPoint

    pieces = []
    inside = run.intersection(skirt)
    if inside.is_empty:
        return []
    geoms = list(inside.geoms) if hasattr(inside, "geoms") else [inside]
    half = pair.width / 2
    for piece in geoms:
        if piece.geom_type != "LineString" or piece.length <= 0:
            continue
        steps = max(2, int(piece.length / _HUG_STEP) + 1)
        distances = [
            float(shape.distance(ShapelyPoint(piece.interpolate(i / steps, normalized=True))))
            - half
            for i in range(steps + 1)
        ]
        nearest = min(distances)
        best = current = 0
        for distance in distances:
            current = current + 1 if distance <= nearest + pair.gap else 0
            best = max(best, current)
        pieces.append((max(best - 1, 0) * piece.length / steps, nearest))
    return pieces


def _bounds(runs: list[Any]) -> tuple[float, float, float, float]:
    xs = [v for run in runs for v in (run.bounds[0], run.bounds[2])]
    ys = [v for run in runs for v in (run.bounds[1], run.bounds[3])]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlaps(
    span: tuple[float, float, float, float], polygon: tuple[Point, ...], margin: float
) -> bool:
    """A cheap bounding-box reject, so the shapely work runs on a handful of shapes."""
    xs = [x for x, _ in polygon]
    ys = [y for _, y in polygon]
    return not (
        max(xs) + margin < span[0]
        or min(xs) - margin > span[2]
        or max(ys) + margin < span[1]
        or min(ys) - margin > span[3]
    )


def _split_pair(
    pair: DiffPair,
    centre: list[Point],
    path: RoutePath,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    congestion: float,
    report: Report,
) -> list[RoutedConnection] | None:
    """Offset a tightened centre-line into two traces, and check it is really a pair."""
    left, right = split_centre_line(centre, pair.pitch)
    assignment = _assign_halves(base, pair, centre, left, right)
    if assignment is None:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()} would have to cross over between its two ends, which "
            "coupled routing does not build yet",
            hint="the halves will be routed separately, so the gap and the skew "
            "budget are no longer guaranteed; swapping the two pads at one end "
            "would remove the crossover",
            net=pair.positive,
        )
        return None

    layer = path.legs[0].layer
    results: list[RoutedConnection] = []
    mate: list[Obstacle] = []
    for net, points, start_pad, end_pad in assignment:
        start_key = base.resolve_pad(start_pad) or start_pad
        end_key = base.resolve_pad(end_pad) or end_pad
        half_rules = rules_for(netlist, net, congestion)
        try:
            head, body, tail = _fan_out(
                base,
                placed,
                mate,
                netlist,
                net,
                layer,
                half_rules,
                replace(half_rules, track_width=pair.width),
                congestion,
                points,
                start_key,
                end_key,
            )
        except (StretchError, FreeSpaceError) as exc:
            report.warning(
                "diff-pair-not-coupled",
                f"{pair.key()} could not be fanned out to its pads: "
                f"{getattr(exc, 'message', exc)}",
                hint="the halves will be routed separately, which keeps the board "
                "legal at the cost of the gap; placing the end components at the "
                "pair's own pitch removes the fan-out entirely",
                net=pair.positive,
            )
            return None
        # Three legs, not one, because they are not the same track. The coupled run
        # carries the pair's own width, which is what the impedance target was
        # calculated for. The fan-out at each end is not coupled to anything, so it
        # carries the class's ordinary trace width -- which is also the only way it
        # can land on a 0.65 mm-pitch receptacle pin at all. Necking a pair down at
        # the connector is what a person does, for the same reason.
        legs: list[StretchResult] = []
        if len(head) > 1:
            legs.append(
                StretchResult(
                    net=net, layer=layer, points=head,
                    width=half_rules.track_width,
                    start=start_key, end=f"{net}<",
                )
            )
        legs.append(
            StretchResult(
                net=net, layer=layer, points=body, width=pair.width,
                crossings=len(centre),
                start=f"{net}<" if len(head) > 1 else start_key,
                end=f"{net}>" if len(tail) > 1 else end_key,
            )
        )
        if len(tail) > 1:
            legs.append(
                StretchResult(
                    net=net, layer=layer, points=tail,
                    width=half_rules.track_width,
                    start=f"{net}>", end=end_key,
                )
            )
        results.append(
            RoutedConnection(net=net, start=start_key, end=end_key, legs=legs)
        )
        # Along the coupled run the two halves are deliberately closer than the
        # class clearance -- that is what `diff_pair_gap_mm` means -- so the first
        # half is not an obstacle to the second there. In the *fan-out* they are no
        # longer a pair, just two nets going to two pads, and ordinary clearance is
        # exactly what should hold between them. So the first half's fan-out, and
        # only its fan-out, is fed back.
        for leg in legs:
            if leg.width != pair.width:
                mate.extend(
                    track_obstacles(
                        leg, f"pair:{net}/{leg.start}>{leg.end}", leg.width / 2
                    )
                )

    uncoupled = _uncoupled_fraction(results, centre)
    if uncoupled > MAX_UNCOUPLED:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()} would be {uncoupled * 100:.0f}% fan-out: its pads are too "
            "far apart at one end for the pair to run coupled between them",
            hint="place the two halves' end components side by side -- a `group` "
            "constraint does that -- or accept the halves being routed separately",
            net=pair.positive,
        )
        return None

    closest = _halves_separation(_polyline(results[0]), _polyline(results[1]))
    if closest < pair.pitch - _GRAZE:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()}'s two halves would come within {closest:.3f} mm of each "
            f"other, against the {pair.pitch:.3f} mm their width and gap need",
            hint="the fan-out at one end brings them back together; moving the end "
            "components further apart, or giving the pair a narrower gap, resolves "
            "it. The halves will be routed separately meanwhile, which keeps the "
            "board legal at the cost of the coupling",
            net=pair.positive,
        )
        return None

    # The tightener guarantees clearance for the centre-line, but the fan-out at
    # each end is constructed rather than tightened, so it has to be checked. A
    # coupled pair that lands DRC violations is worse than two separately routed
    # nets, whatever its gap is.
    offender = _fan_out_collision(
        results,
        base,
        frozenset({pair.positive, pair.negative}),
        layer,
        rules_for(netlist, pair.positive, congestion),
        netlist,
        congestion,
        frozenset(
            key
            for key in (base.resolve_pad(p) for p in (*pair.starts, *pair.ends))
            if key
        ),
    )
    if offender is not None:
        report.warning(
            "diff-pair-not-coupled",
            f"{pair.key()}'s fan-out to its pads would not clear {offender}",
            hint="the halves will be routed separately, which keeps the board legal "
            "at the cost of the gap; placing the end components at the pair's own "
            "pitch removes the fan-out entirely",
            net=pair.positive,
        )
        return None

    report.info(
        "diff-pair-coupled",
        f"{pair.key()} routed as a coupled pair with a {pair.gap} mm gap; "
        f"{uncoupled * 100:.0f}% of each half is fan-out at the ends",
        net=pair.positive,
    )
    _match_lengths(results, pair, base, placed, netlist, congestion, layer, report)
    return results


def _match_lengths(
    results: list[RoutedConnection],
    pair: DiffPair,
    base: RoutingEnvironment,
    placed: list[Obstacle],
    netlist: Netlist,
    congestion: float,
    layer: str,
    report: Report,
) -> None:
    """Meander the shorter half until the pair meets its skew budget.

    Only the fan-out is meandered. The coupled run is where the pair's impedance
    comes from, and folding one half of it up would trade a length mismatch for a
    coupling mismatch -- which is the worse of the two, because it is the one the
    board was designed around.

    The length being matched is the whole conductor, via barrels included. On a
    1.6 mm board a through via is about 1.5 mm of copper, ten times a fast pair's
    budget, so a match that counts only the tracks is matching the wrong thing.
    """
    if pair.max_skew is None or len(results) != 2:
        return
    skew = abs(results[0].length - results[1].length)
    if skew <= pair.max_skew:
        return

    shorter, longer = sorted(results, key=lambda r: r.length)
    rules = rules_for(netlist, shorter.net, congestion)
    mate = [
        obstacle
        for leg in longer.legs
        for obstacle in track_obstacles(
            leg, f"pair:{longer.net}/{leg.start}>{leg.end}", leg.width / 2
        )
    ]
    geometry = geometry_for(
        base,
        [*placed, *mate],
        netlist,
        shorter.net,
        layer,
        rules,
        congestion,
        open_pads=frozenset({shorter.start, shorter.end}),
    )

    from shapely.geometry import LineString

    free = geometry.free

    def fits(candidate: list[Point]) -> bool:
        return bool(free is not None and free.covers(LineString(candidate)))

    # Longest fan-out leg first: the more room there is, the gentler the meander.
    for leg in sorted(
        (leg for leg in shorter.legs if leg.width != pair.width),
        key=lambda leg: -leg.length,
    ):
        folded = lengthen(leg.points, skew, fits, rules.corridor)
        if folded is None:
            continue
        leg.points = folded
        report.info(
            "diff-pair-length-matched",
            f"{pair.key()}: {skew:.3f} mm of meander added to {shorter.net} to meet "
            f"its {pair.max_skew:.3f} mm skew budget",
            net=pair.positive,
        )
        return

    report.warning(
        "diff-pair-skew",
        f"{pair.key()} is {skew:.3f} mm out of length, against a "
        f"{pair.max_skew:.3f} mm budget, and there is no room in "
        f"{shorter.net}'s own corridor to meander the difference away",
        hint="the mismatch comes from the fan-out at the ends; moving the end "
        "components so the two halves break out symmetrically is the fix that does "
        "not cost board area",
        net=pair.positive,
    )


def _fan_out(
    base: RoutingEnvironment,
    placed: list[Obstacle],
    mate: list[Obstacle],
    netlist: Netlist,
    net: str,
    layer: str,
    rules: RouteRules,
    coupled_rules: RouteRules,
    congestion: float,
    coupled: list[Point],
    start_pad: str,
    end_pad: str,
) -> tuple[list[Point], list[Point], list[Point]]:
    """Join a coupled half's offset polyline to the pads at either end.

    The coupled part of a pair comes from tightening one centre-line, so it is legal
    by construction. The fan-out is the bit that is not: at each end the two halves
    leave the pair's own pitch and go to pads that are somewhere else, and a
    straight line from the offset polyline to the pad centre cuts through whatever
    happens to be in between. That is what stopped the `usb-port` pairs coupling in
    M7 -- both refused with the name of the pad their fan-out would have crossed.

    So the fan-out is *tightened* too, as an ordinary short route between two fixed
    points, on the half's own geometry. It goes round what is in the way instead of
    through it, and the pair keeps its gap along the part where the gap means
    something.
    """
    # Two views of the same layer, because the two parts of this half are not the
    # same width. The coupled run carries the pair's width and has to clear
    # everything at *that* width -- including the other half's pad, which the
    # centre-line was allowed to pass because the pair as a whole owns it.
    geometry = geometry_for(
        base, [*placed, *mate], netlist, net, layer, rules, congestion,
        open_pads=frozenset({start_pad, end_pad}),
    )
    coupled_geometry = geometry_for(
        base, placed, netlist, net, layer, coupled_rules, congestion,
        open_pads=frozenset({start_pad, end_pad}),
    )
    start = base.pad_centres.get(start_pad)
    end = base.pad_centres.get(end_pad)
    if start is None or end is None:
        raise StretchError(f"{net}: its pads are not on this board")

    # Where the coupled run can be picked up from. The centre-line was tightened for
    # the *pair's* corridor, so its two ends sit at the pair's own pitch -- which at
    # a pad that is not at that pitch can be inside somebody's clearance. Walking in
    # from each end to the first point that is genuinely free trims exactly the part
    # that was never usable, and the fan-out replaces it.
    body = _clear_span(coupled, coupled_geometry)
    if body is None:
        raise StretchError(
            f"{net}: the coupled run never reaches clear space, so there is nothing "
            "to couple"
        )
    head_point, tail_point = body[0], body[-1]

    head, _ = stretch_guided(
        start, head_point, [], geometry, rules, label=f"pair fan-out {net}"
    )
    tail, _ = stretch_guided(
        tail_point, end, [], geometry, rules, label=f"pair fan-out {net}"
    )
    return head, body, tail


def _clear_span(
    points: list[Point], geometry: LayerGeometry
) -> list[Point] | None:
    """The longest run of a polyline that lies entirely in the routable area.

    The centre-line was tightened for the *pair's* corridor, with both halves' pads
    open to it -- it has to be, or it could not start between them. Offsetting it
    gives each half a line that is legal along the coupled run and not necessarily
    legal at the ends, where it passes the other half's pad at the pair's gap rather
    than at the class's clearance.

    So each half keeps the longest stretch of its offset line that is clear on its
    own terms, and the fan-out covers the rest. Taking the *longest* run rather than
    trimming from each end matters: a pad can jut across the line in the middle, and
    a route that steps over it is not a coupled pair, it is two.
    """
    if len(points) < 2:
        return None
    samples = resample(points, _TRIM_STEP)
    free = [geometry.triangulation.locate(p) is not None for p in samples]

    best = span_start = None
    length = run = 0
    for index, ok in enumerate(free):
        if not ok:
            run, span_start = 0, None
            continue
        if span_start is None:
            span_start = index
        run += 1
        if run > length:
            length, best = run, (span_start, index)
    if best is None or length < 2:
        return None
    first, last = best
    return simplify(samples[first : last + 1])


def _fan_out_collision(
    results: list[RoutedConnection],
    environment: RoutingEnvironment,
    nets: frozenset[str],
    layer: str,
    rules: RouteRules,
    netlist: Netlist,
    congestion: float,
    open_pads: frozenset[str],
) -> str | None:
    """Name the first obstacle a coupled pair's copper would not clear."""
    from shapely.geometry import LineString
    from shapely.geometry import Polygon as ShapelyPolygon

    def blocking_at(width: float) -> list[Obstacle]:
        return environment.blocking(
            nets,
            layer,
            clearance=rules.clearance,
            track_width=width,
            clearance_of=lambda net: rules_for(netlist, net, congestion).clearance
            if net
            else 0.0,
            open_pads=open_pads,
        )

    cache: dict[float, list[Obstacle]] = {}
    for result in results:
        for leg in result.legs:
            if len(leg.points) < 2:
                continue
            # Each leg is checked at its own width. A pair that necks down at its
            # pads has two, and checking the wide part against the narrow part's
            # clearance is how a violation of exactly the difference gets shipped.
            blocking = cache.setdefault(leg.width, blocking_at(leg.width))
            centre = LineString(leg.points)
            for obstacle in blocking:
                if len(obstacle.polygon) < 3:
                    continue
                # The *interior*, not the boundary. Tightening pulls a path against
                # the inflated hulls it goes round -- that is what tightening is --
                # so a path can run along a hull's edge for millimetres while being
                # exactly as legal as one that does not. Testing plain intersection
                # called every correctly tightened fan-out a collision.
                hull = ShapelyPolygon(obstacle.polygon).buffer(-_GRAZE)
                if not hull.is_empty and hull.intersection(centre).length > _GRAZE:
                    return obstacle.name
    return None


def _polyline(connection: RoutedConnection) -> list[Point]:
    """One connection's copper as a single run of points, legs joined end to end."""
    points: list[Point] = []
    for leg in connection.legs:
        points.extend(leg.points if not points else leg.points[1:])
    return points


def _uncoupled_fraction(results: list[RoutedConnection], centre: list[Point]) -> float:
    """How much of each half is fan-out rather than coupled run.

    A pair is only a pair where the two halves are actually side by side. If the
    pads at one end are far apart -- two separate resistors rather than two pins of
    one connector -- most of the "pair" is really two independent breakouts, and the
    impedance and skew guarantees do not hold across them. Measuring the excess of
    each half over the centre-line says exactly how much.
    """
    centre_length = sum(math.dist(a, b) for a, b in pairwise(centre))
    if centre_length <= 0:
        return 1.0
    worst = max(result.copper_length for result in results)
    if worst <= 0:
        return 1.0
    return max(0.0, (worst - centre_length) / worst)


def _midpoint(points: list[Point]) -> Point:
    return (
        (points[0][0] + points[1][0]) / 2,
        (points[0][1] + points[1][1]) / 2,
    )


def _assign_halves(
    environment: RoutingEnvironment,
    pair: DiffPair,
    centre: list[Point],
    left: list[Point],
    right: list[Point],
) -> list[tuple[str, list[Point], str, str]] | None:
    """Decide which offset polyline belongs to which net.

    The choice has to hold at *both* ends. A pair whose pads swap sides between the
    connector and the destination has to cross over somewhere, and a crossover is a
    deliberate construction -- not something that can be had by joining the offsets
    to whichever pad is nearest. When the two ends disagree, this returns ``None``
    and the caller falls back to routing the halves separately, which is worse but
    honest.
    """
    if not left or not right:
        return None
    located: dict[str, Point] = {}
    for name in (*pair.starts, *pair.ends):
        pad = environment.pad_centres.get(environment.resolve_pad(name) or "")
        if pad is None:
            return None
        located[name] = pad

    start_left_is_positive = _same_side(
        centre, left, right, located[pair.starts[0]], at_start=True
    )
    end_left_is_positive = _same_side(
        centre, left, right, located[pair.ends[0]], at_start=False
    )
    if start_left_is_positive is None or end_left_is_positive is None:
        return None
    if start_left_is_positive != end_left_is_positive:
        return None

    near, far = (left, right) if start_left_is_positive else (right, left)
    return [
        (pair.positive, near, pair.starts[0], pair.ends[0]),
        (pair.negative, far, pair.starts[1], pair.ends[1]),
    ]


def _same_side(
    centre: list[Point],
    left: list[Point],
    right: list[Point],
    pad: Point,
    *,
    at_start: bool,
) -> bool | None:
    """Whether the ``left`` offset is the one the positive half's pad sits on.

    Compared by *side* of the centre-line rather than by distance to the offset's
    endpoint. Distance looks like the same question and is not: when a pair's two
    ends are at different spacings -- 0.65 mm at a receptacle, 2 mm at a pair of
    resistors -- both offsets can be nearer the same pad, and the answer flips on a
    hundredth of a millimetre. Which side of the line the pad is on does not flip.
    """
    if len(left) < 2 or len(right) < 2 or len(centre) < 2:
        return None
    index = 0 if at_start else -1
    # The centre-line at this end, and the way the pair is travelling through it.
    # Both the pad and the offset are measured against the same local line, so a
    # bend earlier in the run cannot change the answer.
    anchor = centre[index]
    ahead = centre[1] if at_start else centre[-2]
    direction = (
        (ahead[0] - anchor[0], ahead[1] - anchor[1])
        if at_start
        else (anchor[0] - ahead[0], anchor[1] - ahead[1])
    )
    if math.hypot(*direction) < 1e-9:
        return None

    def side(point: Point) -> float:
        return direction[0] * (point[1] - anchor[1]) - direction[1] * (
            point[0] - anchor[0]
        )

    pad_side = side(pad)
    if abs(pad_side) < 1e-9:
        # The pad sits on the centre-line, so neither half is "its" side. Fall back
        # to whichever offset it is actually nearer, which is what a pair whose pads
        # are in line with its own axis deserves.
        return math.dist(left[index], pad) <= math.dist(right[index], pad)
    return side(left[index]) * pad_side > 0


def _halves_separation(first: list[Point], second: list[Point]) -> float:
    """How close the two halves of a pair come, centre-line to centre-line.

    The coupled run holds its gap by construction. The fan-out at each end does not:
    it is tightened towards two pads that may be closer together than the pair's own
    pitch, and two 0.34 mm traces whose centres converge to 0.36 mm apart are a
    short, not a pair. Measuring it is the only way to know, and measuring it is
    cheap.
    """
    from shapely.geometry import LineString

    if len(first) < 2 or len(second) < 2:
        return 0.0
    return float(LineString(first).distance(LineString(second)))


def _join_to_pads(
    environment: RoutingEnvironment, points: list[Point], start_pad: str, end_pad: str
) -> list[Point]:
    """Fan the offset polyline's ends out to the pads they actually land on."""
    start = environment.pad_centres.get(environment.resolve_pad(start_pad) or "")
    end = environment.pad_centres.get(environment.resolve_pad(end_pad) or "")
    joined = list(points)
    if start is not None:
        joined[0] = start
    if end is not None:
        joined[-1] = end
    return joined

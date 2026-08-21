"""The high-speed verification report (M11e).

**This is rule-based verification, not electromagnetic simulation.** Everything
below is arithmetic over geometry: where the copper is, what is under it, how long
each half is. None of it solves a field, none of it predicts an eye diagram, and a
Gen3 board that passes every check here still wants a human and an SI tool before
anybody signs it off. Saying so is the point of the feature, not a disclaimer
attached to it: a report that reads like a simulation and is not one is worse than
no report.

What it does check, all of it measured off the board KiCad's own DRC ran against:

* **reference continuity** -- every controlled-impedance track projected onto the
  plane its class declares, with every stretch that has no plane under it, and
  every stretch where the plane under it belongs to a different net, reported with
  its length. This is the classic return-path check, and it is the reason M10's
  fill had to come first: an unfilled zone has no copper to project onto;
* **geometry audit** -- the width the board was actually written with, and the
  separation the two halves actually hold, against the geometry the impedance
  target derived;
* **skew and length** -- per pair, after meanders;
* **via stubs** -- for each via on a controlled-impedance net, the barrel left over
  below the layers the signal uses;
* **coupling** -- uncoupled length against the class's budget, from what M11d
  measured while it routed.

Severity is *warning* for a finding and *info* for a measurement, because these are
engineering-judgement items rather than rule violations. A net class that wants
them to fail the check says ``verify: error``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aipcb.checks.planes import filled_copper
from aipcb.diagnostics import Report, Severity
from aipcb.highspeed import GEOMETRY_TOLERANCE, ImpedanceTarget, controlled_classes
from aipcb.kicad.sexpr import SNode
from aipcb.model.layout import Stackup, copper_layer_names
from aipcb.netlist import Netlist
from aipcb.route.pairs import PairAudit

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

__all__ = [
    "STUB_WARN_MM",
    "HighSpeedReport",
    "analyse_highspeed",
    "report_highspeed",
]

Point = tuple[float, float]

#: How much via stub a controlled-impedance net may carry before it is reported, in
#: millimetres. A through via on a 1.6 mm board that exits on an inner layer leaves
#: most of the barrel behind as a resonant stub; half a millimetre is where the
#: usual guidance starts to care at multi-gigabit rates.
STUB_WARN_MM = 0.5

#: How finely a track is sampled when asking what is underneath it, in millimetres.
#: Fine enough that a 0.2 mm slot in a plane cannot hide between two samples.
_PROJECT_STEP = 0.05

#: Stretches shorter than this are not findings. A track crossing a plane's own
#: hairline slit -- KiCad writes a filled polygon with a hole as several outlines
#: joined by one -- would otherwise be reported as a gap in the reference.
_MIN_FINDING_MM = 0.05


@dataclass(frozen=True, slots=True)
class ReferenceGap:
    """A stretch of track with no reference under it, or the wrong reference."""

    net: str
    layer: str
    reference: str
    kind: str
    """``void`` -- no copper at all; ``split`` -- copper of another net."""
    length_mm: float
    at: Point
    other_net: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "net": self.net,
            "layer": self.layer,
            "reference": self.reference,
            "kind": self.kind,
            "length_mm": round(self.length_mm, 4),
            "at": [round(self.at[0], 4), round(self.at[1], 4)],
            "other_net": self.other_net,
        }


@dataclass(frozen=True, slots=True)
class ViaStub:
    """One via on a controlled-impedance net, and the barrel it leaves behind."""

    net: str
    at: Point
    span: tuple[str, str]
    used: tuple[str, str]
    stub_mm: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "net": self.net,
            "at": [round(self.at[0], 4), round(self.at[1], 4)],
            "barrel": list(self.span),
            "used": list(self.used),
            "stub_mm": round(self.stub_mm, 4),
        }


@dataclass(frozen=True, slots=True)
class PairGeometry:
    """What one pair actually came out as, against what it was asked for."""

    key: str
    net_class: str
    layer: str
    coupled: bool
    target_ohm: float | None
    target_width_mm: float
    target_gap_mm: float
    actual_widths_mm: tuple[float, ...]
    actual_gap_mm: float | None
    coupled_length_mm: float
    uncoupled_mm: tuple[float, ...]
    budget_mm: float | None
    skew_mm: float | None
    max_skew_mm: float | None

    @property
    def width_deviation(self) -> float:
        """The worst fractional distance of any coupled-run width from the target."""
        if not self.actual_widths_mm or self.target_width_mm <= 0:
            return 0.0
        return max(
            (width - self.target_width_mm) / self.target_width_mm
            for width in self.actual_widths_mm
        )

    @property
    def gap_deviation(self) -> float:
        if self.actual_gap_mm is None or self.target_gap_mm <= 0:
            return 0.0
        return (self.actual_gap_mm - self.target_gap_mm) / self.target_gap_mm

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.key,
            "net_class": self.net_class,
            "layer": self.layer,
            "coupled": self.coupled,
            "target_ohm": self.target_ohm,
            "target_width_mm": self.target_width_mm,
            "target_gap_mm": self.target_gap_mm,
            "actual_widths_mm": [round(w, 4) for w in self.actual_widths_mm],
            "actual_gap_mm": (
                None if self.actual_gap_mm is None else round(self.actual_gap_mm, 4)
            ),
            "width_deviation": round(self.width_deviation, 4),
            "gap_deviation": round(self.gap_deviation, 4),
            "coupled_length_mm": round(self.coupled_length_mm, 4),
            "uncoupled_mm": [round(v, 4) for v in self.uncoupled_mm],
            "max_uncoupled_mm": self.budget_mm,
            "skew_mm": None if self.skew_mm is None else round(self.skew_mm, 4),
            "max_skew_mm": self.max_skew_mm,
        }


@dataclass(slots=True)
class HighSpeedReport:
    """Everything M11e measured, in one object."""

    pairs: list[PairGeometry] = field(default_factory=list)
    gaps: list[ReferenceGap] = field(default_factory=list)
    stubs: list[ViaStub] = field(default_factory=list)
    projected_mm: float = 0.0
    """Total controlled-impedance track length projected onto a reference plane."""
    reference_checked: bool = False
    """False when there was no filled board to project onto."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "rule-based geometry checks, not electromagnetic simulation",
            "reference_checked": self.reference_checked,
            "projected_mm": round(self.projected_mm, 4),
            "pairs": [pair.to_dict() for pair in self.pairs],
            "reference_gaps": [gap.to_dict() for gap in self.gaps],
            "via_stubs": [stub.to_dict() for stub in self.stubs],
        }


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def analyse_highspeed(
    board: SNode,
    netlist: Netlist,
    audits: list[PairAudit],
    skew: dict[str, float],
    *,
    filled: bool = True,
) -> HighSpeedReport:
    """Measure every controlled-impedance net on a routed -- and filled -- board."""
    targets = controlled_classes(netlist)
    if not targets:
        return HighSpeedReport()

    stackup = netlist.layout.stackup if netlist.layout is not None else Stackup()
    tracks = _tracks(board)
    result = HighSpeedReport(reference_checked=filled)

    controlled = {
        name: targets[net.net_class]
        for name, net in netlist.nets.items()
        if net.net_class in targets
    }
    if filled:
        copper = filled_copper(board)
        for name in sorted(controlled):
            _project(name, controlled[name], tracks.get(name, []), copper, result)

    result.stubs = _stubs(board, controlled, stackup)
    result.pairs = _pairs(audits, targets, tracks, skew, netlist)
    return result


def _tracks(board: SNode) -> dict[str, list[tuple[str, float, Point, Point]]]:
    """Every track segment, by net: ``(layer, width, start, end)``."""
    names = {
        code: name
        for node in board.children("net")
        if (code := node.value(0)) is not None and (name := node.value(1)) is not None
    }
    out: dict[str, list[tuple[str, float, Point, Point]]] = {}
    for item in board.children("segment"):
        start, end = item.child("start"), item.child("end")
        width, layer = item.child("width"), item.get("layer")
        code = item.get("net")
        net = names.get(code) if code is not None else None
        if start is None or end is None or layer is None or not net:
            continue
        out.setdefault(net, []).append(
            (
                layer,
                float(width.value(0) or 0.25) if width is not None else 0.25,
                (float(start.value(0) or 0), float(start.value(1) or 0)),
                (float(end.value(0) or 0), float(end.value(1) or 0)),
            )
        )
    return out


def _project(
    net: str,
    target: ImpedanceTarget,
    segments: list[tuple[str, float, Point, Point]],
    copper: dict[str, dict[str, BaseGeometry]],
    result: HighSpeedReport,
) -> None:
    """Walk a net's tracks and ask, every 50 um, what is underneath.

    The reference is the plane the *class* declares. A segment that has wandered
    onto another layer is projected onto that layer's own nearest plane instead,
    and the change of reference is itself worth knowing -- which is what the
    ``split`` finding says when the two planes carry different nets.
    """
    from shapely.geometry import Point as ShapelyPoint

    if target.reference is None:
        return
    reference = copper.get(target.reference, {})
    if not reference:
        return
    # The plane's own net, by area: a reference layer normally carries one pour,
    # and where it carries two the larger is the reference and the smaller is the
    # split this check exists to find.
    home = max(reference, key=lambda name: float(reference[name].area))

    run: list[tuple[str, str | None, Point, float]] = []
    for layer, _width, start, end in sorted(segments):
        length = math.dist(start, end)
        if length <= 0:
            continue
        steps = max(1, int(length / _PROJECT_STEP))
        for index in range(steps + 1):
            t = index / steps
            here = (
                start[0] + (end[0] - start[0]) * t,
                start[1] + (end[1] - start[1]) * t,
            )
            probe = ShapelyPoint(here)
            under = next(
                (name for name, shape in sorted(reference.items()) if shape.covers(probe)),
                None,
            )
            step = length / steps
            result.projected_mm += step
            if under == home:
                _flush(net, layer, target.reference, run, result)
                run = []
            else:
                run.append(("void" if under is None else "split", under, here, step))
        _flush(net, layer, target.reference, run, result)
        run = []


def _flush(
    net: str,
    layer: str,
    reference: str,
    run: list[tuple[str, str | None, Point, float]],
    result: HighSpeedReport,
) -> None:
    if not run:
        return
    length = sum(step for _, _, _, step in run)
    if length < _MIN_FINDING_MM:
        return
    kind = "split" if any(k == "split" for k, _, _, _ in run) else "void"
    other = next((name for k, name, _, _ in run if k == "split"), None)
    result.gaps.append(
        ReferenceGap(
            net=net,
            layer=layer,
            reference=reference,
            kind=kind,
            length_mm=length,
            at=run[0][2],
            other_net=other,
        )
    )


def _stubs(
    board: SNode,
    controlled: dict[str, ImpedanceTarget],
    stackup: Stackup,
) -> list[ViaStub]:
    """The barrel a via leaves behind, below and above the layers carrying signal.

    Only a *through* via has a stub worth the name: it is drilled from one outer
    layer to the other whatever the file says its span is, so a signal that enters
    on the front and leaves on an inner layer leaves the rest of the barrel hanging.
    A blind or buried via ends where it ends.
    """
    names = {
        code: name
        for node in board.children("net")
        if (code := node.value(0)) is not None and (name := node.value(1)) is not None
    }
    order = copper_layer_names(stackup.copper_layers)
    outer_top, outer_bottom = order[0], order[-1]
    through_only = "through" in stackup.via_types and len(stackup.via_types) == 1

    out: list[ViaStub] = []
    for item in board.children("via"):
        code = item.get("net")
        net = names.get(code) if code is not None else None
        if not net or net not in controlled:
            continue
        layers = item.child("layers")
        at = item.child("at")
        if layers is None or at is None:
            continue
        atoms = [str(a.value) for a in layers.atoms()]
        if len(atoms) != 2 or atoms[0] not in order or atoms[1] not in order:
            continue
        used = (atoms[0], atoms[1])
        blind = any(str(a.value) in ("blind", "micro") for a in item.atoms())
        span = used if (blind and not through_only) else (outer_top, outer_bottom)
        shallow, deep = sorted(used, key=order.index)
        stub = stackup.barrel_length_mm(span[0], shallow) + stackup.barrel_length_mm(
            deep, span[1]
        )
        out.append(
            ViaStub(
                net=net,
                at=(float(at.value(0) or 0), float(at.value(1) or 0)),
                span=span,
                used=used,
                stub_mm=stub,
            )
        )
    return sorted(out, key=lambda v: (-v.stub_mm, v.net, v.at))


def _pairs(
    audits: list[PairAudit],
    targets: dict[str, ImpedanceTarget],
    tracks: dict[str, list[tuple[str, float, Point, Point]]],
    skew: dict[str, float],
    netlist: Netlist,
) -> list[PairGeometry]:
    out: list[PairGeometry] = []
    for audit in audits:
        target = targets.get(audit.net_class)
        if target is None:
            continue
        rules = netlist.net_classes.get(audit.net_class)
        widths = tuple(
            sorted(
                {
                    round(width, 4)
                    for net in (audit.positive, audit.negative)
                    for _layer, width, _a, _b in tracks.get(net, [])
                    if abs(width - audit.width_mm) < 1e-6
                }
            )
        ) or tuple(
            sorted(
                {
                    round(width, 4)
                    for net in (audit.positive, audit.negative)
                    for _layer, width, _a, _b in tracks.get(net, [])
                }
            )
        )
        gap, coupled_length = _measure_gap(audit, tracks)
        out.append(
            PairGeometry(
                key=audit.key,
                net_class=audit.net_class,
                layer=audit.layer,
                coupled=audit.coupled,
                target_ohm=audit.target_ohm,
                target_width_mm=target.width_mm,
                target_gap_mm=target.gap_mm,
                actual_widths_mm=widths,
                actual_gap_mm=gap,
                coupled_length_mm=coupled_length,
                uncoupled_mm=audit.uncoupled_mm,
                budget_mm=audit.budget_mm,
                skew_mm=skew.get(audit.key),
                max_skew_mm=rules.max_skew_mm if rules is not None else None,
            )
        )
    return out


def _measure_gap(
    audit: PairAudit, tracks: dict[str, list[tuple[str, float, Point, Point]]]
) -> tuple[float | None, float]:
    """The separation the two halves actually hold along their coupled run.

    Measured edge to edge off the emitted copper, not taken from what was asked
    for: the whole point of an audit is that it can disagree with the intent.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    runs = {}
    for net in (audit.positive, audit.negative):
        pieces = [
            LineString([a, b])
            for _layer, width, a, b in tracks.get(net, [])
            if abs(width - audit.width_mm) < 1e-6 and a != b
        ]
        if not pieces:
            return (None, 0.0)
        runs[net] = unary_union(pieces)
    first, second = runs[audit.positive], runs[audit.negative]
    length = float(first.length)
    if length <= 0:
        return (None, 0.0)

    steps = max(2, int(length / max(_PROJECT_STEP * 4, 1e-6)))
    samples = [
        float(second.distance(first.interpolate(i / steps, normalized=True)))
        for i in range(steps + 1)
    ]
    inside = sorted(samples)[len(samples) // 10 : len(samples) - len(samples) // 10]
    if not inside:
        return (None, length)
    median = inside[len(inside) // 2]
    return (median - audit.width_mm, length)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def report_highspeed(
    result: HighSpeedReport, netlist: Netlist, report: Report
) -> None:
    """Turn the measurements into diagnostics pointing at the classes that made them."""
    if not result.pairs and not result.gaps and not result.stubs:
        return

    for pair in result.pairs:
        _report_pair(pair, netlist, report)
    _report_reference(result, netlist, report)
    _report_stubs(result, netlist, report)


def _severity(netlist: Netlist, net_class: str) -> Severity:
    rules = netlist.net_classes.get(net_class)
    if rules is not None and rules.verify == "error":
        return Severity.ERROR
    return Severity.WARNING


def _loc(netlist: Netlist, net_class: str) -> Any:
    return netlist.locs.get(("net_classes", net_class))


def _report_pair(pair: PairGeometry, netlist: Netlist, report: Report) -> None:
    path: tuple[str | int, ...] = ("net_classes", pair.net_class)
    widths = ", ".join(f"{w:g}" for w in pair.actual_widths_mm) or "none"
    report.info(
        "hs-pair-geometry",
        f"{pair.key}: {pair.target_ohm:.0f} ohm target, "
        f"{pair.target_width_mm:g}/{pair.target_gap_mm:g} mm derived; the board "
        f"carries {widths} mm wide"
        + (
            f" at a measured {pair.actual_gap_mm:.3f} mm gap"
            if pair.actual_gap_mm is not None
            else " and was not coupled"
        )
        + f", {pair.coupled_length_mm:.2f} mm coupled",
        loc=_loc(netlist, pair.net_class),
        path=path,
        pair=pair.key,
    )

    if abs(pair.width_deviation) > GEOMETRY_TOLERANCE:
        report.add(
            _severity(netlist, pair.net_class),
            "hs-width-deviation",
            f"{pair.key} is {pair.width_deviation:+.0%} off its "
            f"{pair.target_width_mm:g} mm impedance-derived width",
            loc=_loc(netlist, pair.net_class),
            path=path,
            hint="the width the board is built with comes from the source; this is "
            "the report saying what that costs against the target",
            pair=pair.key,
        )
    if pair.actual_gap_mm is not None and abs(pair.gap_deviation) > GEOMETRY_TOLERANCE:
        report.add(
            _severity(netlist, pair.net_class),
            "hs-gap-deviation",
            f"{pair.key} holds a {pair.actual_gap_mm:.3f} mm gap along its coupled "
            f"run, {pair.gap_deviation:+.0%} from the {pair.target_gap_mm:g} mm the "
            "impedance was derived at",
            loc=_loc(netlist, pair.net_class),
            path=path,
            hint="a pair that has to open its gap round an obstacle is a pair with "
            "a section at the wrong impedance",
            pair=pair.key,
        )
    if pair.budget_mm is not None and pair.uncoupled_mm:
        worst = max(pair.uncoupled_mm)
        report.info(
            "hs-coupling",
            f"{pair.key}: {worst:.3f} mm uncoupled against a {pair.budget_mm:g} mm "
            f"budget ({worst / pair.budget_mm:.0%} of it)",
            loc=_loc(netlist, pair.net_class),
            path=path,
            pair=pair.key,
        )
    if (
        pair.skew_mm is not None
        and pair.max_skew_mm is not None
        and pair.skew_mm > pair.max_skew_mm
    ):
        report.add(
            _severity(netlist, pair.net_class),
            "hs-skew",
            f"{pair.key} is {pair.skew_mm:.3f} mm out of length against its "
            f"{pair.max_skew_mm:g} mm budget, after meanders",
            loc=_loc(netlist, pair.net_class),
            path=path,
            pair=pair.key,
        )


def _report_reference(
    result: HighSpeedReport, netlist: Netlist, report: Report
) -> None:
    if not result.reference_checked:
        report.info(
            "hs-reference-unchecked",
            "no filled board was available, so no controlled-impedance track was "
            "projected onto its reference plane",
            hint="the reference check reads copper the zone filler produced; a "
            "design with no `pours:` has none",
        )
        return
    if not result.gaps:
        report.info(
            "hs-reference-continuous",
            f"{result.projected_mm:.1f} mm of controlled-impedance track projected "
            "onto its declared reference plane, with continuous plane copper of one "
            "net under all of it",
            hint="this is a geometric projection, not a field solve: it says the "
            "return path has somewhere to go, not that it goes there",
        )
        return
    by_net: dict[str, list[ReferenceGap]] = {}
    for gap in result.gaps:
        by_net.setdefault(gap.net, []).append(gap)
    for net in sorted(by_net):
        found = sorted(by_net[net], key=lambda g: -g.length_mm)
        total = sum(g.length_mm for g in found)
        elaborated = netlist.nets.get(net)
        net_class = elaborated.net_class if elaborated is not None else "signal"
        where = "; ".join(
            f"{g.kind} of {g.length_mm:.3f} mm at [{g.at[0]:.2f}, {g.at[1]:.2f}]"
            + (f" over {g.other_net}" if g.other_net else "")
            for g in found[:4]
        )
        report.add(
            _severity(netlist, net_class),
            "hs-reference-broken",
            f"{net} crosses {len(found)} break{'s' if len(found) != 1 else ''} in "
            f"{found[0].reference}, {total:.3f} mm in total: {where}",
            loc=_loc(netlist, net_class),
            path=("net_classes", net_class, "reference"),
            hint="the return current follows the signal; where the plane under the "
            "track stops, or changes net, the return has to go round and the loop "
            "the pair encloses is no longer the one it was designed for",
            net=net,
        )


def _report_stubs(result: HighSpeedReport, netlist: Netlist, report: Report) -> None:
    if not result.stubs:
        return
    worst = result.stubs[0]
    over = [stub for stub in result.stubs if stub.stub_mm > STUB_WARN_MM]
    report.info(
        "hs-via-stubs",
        f"{len(result.stubs)} via{'s' if len(result.stubs) != 1 else ''} on "
        f"controlled-impedance nets; the longest stub is {worst.stub_mm:.3f} mm on "
        f"{worst.net} against a {STUB_WARN_MM} mm threshold",
        hint="a through via's stub is the barrel below the layer the signal leaves "
        "on; back-drilling removes it and is not something this toolchain asks for",
    )
    for stub in over:
        elaborated = netlist.nets.get(stub.net)
        net_class = elaborated.net_class if elaborated is not None else "signal"
        report.add(
            _severity(netlist, net_class),
            "hs-via-stub",
            f"{stub.net}: a via at [{stub.at[0]:.2f}, {stub.at[1]:.2f}] carries "
            f"signal between {stub.used[0]} and {stub.used[1]} through a barrel "
            f"spanning {stub.span[0]} to {stub.span[1]}, leaving "
            f"{stub.stub_mm:.3f} mm of stub against the {STUB_WARN_MM} mm threshold",
            loc=_loc(netlist, net_class),
            path=("net_classes", net_class),
            hint="move the transition to the outer layers, ask the fabricator for "
            "back-drilling, or accept the resonance with the number in front of you",
            net=stub.net,
        )

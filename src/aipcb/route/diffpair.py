"""What a differential pair *is*: which nets, which pads, how wide, how far apart.

A pair is not two nets that happen to run alongside each other. Its impedance comes
from the coupling between them, so the gap has to hold along the whole run and the
two halves have to stay the same length. Routing them independently and hoping does
not produce either property.

This module answers the questions that come before any routing: which declared pairs
can be routed as pairs at all, which pad of each net faces which, and what gap the
impedance target implies when the source does not state one. Turning that into
copper is :mod:`aipcb.route.pairs`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString

from aipcb.diagnostics import Report
from aipcb.impedance import hammerstad_microstrip
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.obstacles import RoutingEnvironment

__all__ = [
    "DiffPair",
    "ImpedanceUnreachable",
    "achievable_range",
    "estimate_gap",
    "find_pairs",
    "geometry_for_class",
    "split_centre_line",
]

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class DiffPair:
    """Two nets that must be routed together."""

    positive: str
    negative: str
    starts: tuple[str, str]
    """The pads at one end, positive first."""
    ends: tuple[str, str]
    """The pads at the other end."""
    width: float
    gap: float
    max_skew: float | None = None
    net_class: str = ""
    """The class both halves belong to, for looking up its high-speed rules."""
    max_uncoupled: float | None = None
    """M11d rule 3: how much of each half may run uncoupled, in millimetres."""
    standoff: float = 1.0
    """M11d rule 1: the clearance multiplier this pair is tightened against."""
    target_ohm: float | None = None
    """The differential impedance the geometry was derived for, if any."""

    @property
    def pitch(self) -> float:
        """Centre-to-centre spacing of the two traces."""
        return self.width + self.gap

    @property
    def corridor(self) -> float:
        """The width the pair occupies together."""
        return 2 * self.width + self.gap

    def key(self) -> str:
        return f"{self.positive}+{self.negative}"


def find_pairs(
    netlist: Netlist, environment: RoutingEnvironment, report: Report
) -> list[DiffPair]:
    """Find the differential pairs that can be routed as pairs.

    A pair needs exactly two pads on each net, so the two ends are unambiguous.
    Anything else -- a pair with a stub, a three-pad net -- is left to ordinary
    routing and said so, because guessing which pads face each other would silently
    produce a pair coupled to the wrong thing.
    """
    pairs: list[DiffPair] = []
    seen: set[str] = set()

    for name in sorted(netlist.nets):
        net = netlist.nets[name]
        partner_name = net.attrs.diff_pair
        if not partner_name or name in seen or partner_name in seen:
            continue
        partner = netlist.nets.get(partner_name)
        if partner is None:
            continue

        positive, negative = sorted((name, partner_name))
        pads = {
            side: sorted(
                key
                for key, pad_net in environment.pad_nets.items()
                if pad_net == side_name
            )
            for side, side_name in (("p", positive), ("n", negative))
        }
        if len(pads["p"]) != 2 or len(pads["n"]) != 2:
            report.info(
                "diff-pair-not-coupled",
                f"{positive}/{negative} has {len(pads['p'])} and {len(pads['n'])} "
                "pads, so its two ends are ambiguous; routed as ordinary nets",
                hint="coupled routing needs exactly two pads on each half",
                net=positive,
            )
            seen.update((positive, negative))
            continue

        starts, ends = _match_ends(pads["p"], pads["n"], environment)
        net_class = netlist.net_classes.get(net.net_class, NetClass())
        width, gap = geometry_for_class(
            netlist, net.net_class, net_class, net, report, positive
        )

        pairs.append(
            DiffPair(
                positive=positive,
                negative=negative,
                starts=starts,
                ends=ends,
                width=width,
                gap=gap,
                max_skew=net_class.max_skew_mm,
                net_class=net.net_class,
                max_uncoupled=net_class.max_uncoupled_mm,
                standoff=net_class.standoff,
                target_ohm=net_class.impedance_diff_ohm,
            )
        )
        seen.update((positive, negative))

    return pairs


def _match_ends(
    positive: list[str], negative: list[str], environment: RoutingEnvironment
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Decide which pad of each net faces which, by total distance."""
    centre = environment.pad_centres
    straight = math.dist(centre[positive[0]], centre[negative[0]]) + math.dist(
        centre[positive[1]], centre[negative[1]]
    )
    crossed = math.dist(centre[positive[0]], centre[negative[1]]) + math.dist(
        centre[positive[1]], centre[negative[0]]
    )
    if straight <= crossed:
        return (positive[0], negative[0]), (positive[1], negative[1])
    return (positive[0], negative[1]), (positive[1], negative[0])


def geometry_for_class(
    netlist: Netlist,
    class_name: str,
    net_class: NetClass,
    net: object,
    report: Report,
    name: str,
) -> tuple[float, float]:
    """The width and gap a pair of this class is routed at.

    A controlled-impedance class (M11a) derives both from its stackup, and an
    explicit ``diff_pair_width_mm`` or ``diff_pair_gap_mm`` overrides what was
    derived rather than being ignored -- ``aipcb validate`` is where the
    disagreement is reported, so that the board is built from what the source
    actually says. Everything else keeps the M8 behaviour exactly.
    """
    from aipcb.highspeed import target_for

    target = target_for(netlist, class_name, net_class)
    if target is not None:
        if target.unreachable is not None:
            report.warning(
                "diff-pair-impedance-unreachable",
                f"{name}: {target.unreachable}",
                hint="change the gap, the stackup or the reference layer; the pair "
                "is routed at the class's declared width and gap meanwhile",
                net=name,
            )
        return target.width_mm, target.gap_mm

    width = net_class.diff_pair_width_mm or net_class.trace_width_mm
    return width, _gap_for(net, net_class, netlist, report, name)


def _gap_for(
    net: object, net_class: NetClass, netlist: Netlist, report: Report, name: str
) -> float:
    """The gap between the two halves, from the rules or from the impedance target."""
    if net_class.diff_pair_gap_mm is not None:
        return net_class.diff_pair_gap_mm

    target = net_class.impedance_ohm or getattr(getattr(net, "attrs", None), "impedance_ohm", None)
    if target is None:
        return net_class.clearance_mm

    width = net_class.diff_pair_width_mm or net_class.trace_width_mm
    height = _dielectric_height(netlist)
    try:
        gap = estimate_gap(target, width, height)
    except ImpedanceUnreachable as exc:
        report.warning(
            "diff-pair-impedance-unreachable",
            f"{name}: {exc}",
            hint=f"widen the pair with `diff_pair_width_mm` (currently {width} mm), "
            "change the stackup, or set `diff_pair_gap_mm` explicitly and accept "
            "the impedance that results",
            net=name,
        )
        return net_class.clearance_mm
    report.warning(
        "diff-pair-gap-estimated",
        f"{name} asks for {target:.0f} ohm differential but gives no "
        f"`diff_pair_gap_mm`; using an estimated {gap:.3f} mm",
        hint="this is a closed-form approximation for edge-coupled microstrip, not "
        "a field solve; confirm it against your fabricator's stackup and set "
        "`diff_pair_gap_mm` explicitly",
        net=name,
    )
    return gap


def _dielectric_height(netlist: Netlist) -> float:
    """Distance from the outer copper to the reference plane below it.

    A two-layer board references the far side; more layers put a plane one
    dielectric away. Either way this is the height the coupling sees, and it is the
    same number the stackup is written with -- an impedance estimated against a
    different board than the one that gets fabricated is worse than none.
    """
    if netlist.layout is None:
        return 0.2
    return max(netlist.layout.stackup.dielectric_thickness_mm, 0.05)


class ImpedanceUnreachable(ValueError):
    """The impedance target cannot be met by spacing alone."""

    def __init__(self, target: float, low: float, high: float) -> None:
        super().__init__(
            f"{target:.0f} ohm differential is outside the {low:.0f}-{high:.0f} ohm "
            "range this trace width and stackup can reach by spacing alone"
        )
        self.target = target
        self.low = low
        self.high = high


def achievable_range(width: float, height: float) -> tuple[float, float]:
    """The differential impedance a given trace width and stackup can span.

    Spacing only moves the impedance between two limits: tightly coupled at the
    bottom, effectively uncoupled at the top. A target outside that band cannot be
    reached by moving the traces, however far apart they go.
    """
    z0 = _microstrip_impedance(width, height)
    return (2 * z0 * (1 - 0.48), 2 * z0)


def estimate_gap(target_ohm: float, width: float, height: float) -> float:
    """Estimate the gap that gives ``target_ohm`` differential, in millimetres.

    Uses the standard closed form for edge-coupled microstrip,

        Z_diff = 2 * Z0 * (1 - 0.48 * exp(-0.96 * s / h))

    solved for the spacing ``s``, with ``Z0`` from the Hammerstad single-ended
    approximation. This is an estimate and is reported as one: real controlled
    impedance depends on copper thickness, solder mask, etch tolerance and the
    laminate the fabricator actually has in stock. It exists so a design with an
    impedance target and no explicit gap produces something sane rather than
    silently falling back to the default clearance.

    Raises :class:`ImpedanceUnreachable` when spacing cannot get there at all --
    which is a real answer, not a failure: it means the trace width or the stackup
    has to change, and clamping to the nearest gap would hide that.
    """
    low, high = achievable_range(width, height)
    if not low <= target_ohm <= high:
        raise ImpedanceUnreachable(target_ohm, low, high)

    z0 = _microstrip_impedance(width, height)
    ratio = 1.0 - target_ohm / (2.0 * z0)
    spacing = -math.log(max(ratio, 1e-6) / 0.48) * height / 0.96
    return round(max(spacing, 0.05), 3)


def _microstrip_impedance(width: float, height: float, epsilon_r: float = 4.3) -> float:
    """Hammerstad's approximation for the impedance of a single microstrip.

    Kept on the M8 path deliberately; :mod:`aipcb.impedance` explains why the
    controlled-impedance path uses IPC-2141 instead and what the two disagree by.
    """
    return hammerstad_microstrip(width, height, epsilon_r)


# ---------------------------------------------------------------------------
# splitting a centre line into two traces
# ---------------------------------------------------------------------------


def split_centre_line(
    centre: list[Point], pitch: float
) -> tuple[list[Point], list[Point]]:
    """Offset a centre-line to either side by half the pitch.

    Shapely does the mitring, which is the part worth not writing by hand: at a
    corner the inside trace has to cut in and the outside has to swing wide, and
    getting that wrong puts the gap out of specification exactly where a pair is
    most sensitive to it.
    """
    if len(centre) < 2:
        return list(centre), list(centre)

    line = LineString(centre)
    half = pitch / 2
    left = _offset(line, half)
    right = _offset(line, -half)
    return left, right


def _offset(line: LineString, distance: float) -> list[Point]:
    """One side of the centre-line.

    Note that Shapely's ``offset_curve`` preserves the input direction for both
    signs -- unlike the older ``parallel_offset``, which reversed the right-hand
    side. Reversing it "back" leaves one half of the pair running end-to-start,
    which then gets joined to the wrong pads and doubles the length of that half.
    """
    offset = line.offset_curve(distance, join_style="mitre", mitre_limit=4.0)
    if offset.is_empty:
        return list(line.coords)
    if offset.geom_type == "MultiLineString":
        offset = max(offset.geoms, key=lambda g: g.length)
    return [(round(x, 6), round(y, 6)) for x, y in offset.coords]

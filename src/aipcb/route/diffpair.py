"""Differential pairs: routed as one thing, emitted as two.

A pair is not two nets that happen to run alongside each other. Its impedance comes
from the coupling between them, so the gap has to hold along the whole run and the
two halves have to stay the same length. Routing them independently and hoping does
not produce either property.

So a pair is tightened *once*, as a single centre-line wide enough for both traces
and the gap between them, and the result is then offset to either side. The gap is
then correct by construction everywhere, including around corners, and the two
halves are the same length except where the offset makes the outside of a bend
longer -- which is exactly the skew a real pair accumulates, and is measured and
reported rather than assumed away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

from shapely.geometry import LineString

from aipcb.diagnostics import Report
from aipcb.model.layout import NetClass
from aipcb.netlist import Netlist
from aipcb.route.obstacles import RoutingEnvironment
from aipcb.route.stretch import StretchResult

__all__ = [
    "DiffPair",
    "ImpedanceUnreachable",
    "achievable_range",
    "estimate_gap",
    "find_pairs",
    "skew_of",
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
        width = net_class.diff_pair_width_mm or net_class.trace_width_mm
        gap = _gap_for(net, net_class, netlist, report, positive)

        pairs.append(
            DiffPair(
                positive=positive,
                negative=negative,
                starts=starts,
                ends=ends,
                width=width,
                gap=gap,
                max_skew=net_class.max_skew_mm,
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
    """Distance from the outer copper to the reference plane below it."""
    if netlist.layout is None:
        return 0.2
    stack = netlist.layout.stackup
    # A two-layer board references the far side; more layers put a plane one
    # dielectric away. Either way this is the height the coupling sees.
    copper = 0.035 * stack.copper_layers
    dielectrics = max(stack.copper_layers - 1, 1)
    return max((stack.thickness_mm - copper) / dielectrics, 0.05)


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
    """Hammerstad's approximation for the impedance of a single microstrip."""
    effective = (epsilon_r + 1) / 2 + (epsilon_r - 1) / 2 * (
        1 / math.sqrt(1 + 12 * height / max(width, 1e-6))
    )
    ratio = width / max(height, 1e-6)
    if ratio < 1:
        return (60 / math.sqrt(effective)) * math.log(8 / ratio + ratio / 4)
    return (120 * math.pi / math.sqrt(effective)) / (
        ratio + 1.393 + 0.667 * math.log(ratio + 1.444)
    )


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


def skew_of(a: StretchResult, b: StretchResult) -> float:
    """The length difference between the two halves of a pair, in millimetres."""
    return abs(a.length - b.length)


@dataclass(slots=True)
class PairOutcome:
    """What routing one pair produced."""

    pair: DiffPair
    results: list[tuple[StretchResult, str, str]] = field(default_factory=list)
    skew: float = 0.0
    centre_length: float = 0.0


def route_length(points: list[Point]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))

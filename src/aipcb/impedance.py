"""Closed-form impedance approximations, in one place.

Three formulas live here, and the differences between them matter enough to be
written down rather than discovered.

**Hammerstad** (:func:`hammerstad_microstrip`) is what M8 used to estimate a gap
from an ``impedance_ohm`` target. It ignores copper thickness, which is a
reasonable simplification when the trace is much wider than the foil is thick.

**IPC-2141** (:func:`ipc2141_microstrip`) includes the copper thickness in the
denominator, which is where a 0.24 mm trace on 35 um foil differs from a 0.24 mm
trace on nothing. On the M11 reference stackup the two disagree by about 8 %: a
0.32 mm pair at a 0.2 mm gap over 0.2104 mm of prepreg comes out at 85 ohm by
IPC-2141 and 93 ohm by Hammerstad. Neither is a field solve and both are
documented as estimates, but a *derivation* and the *audit* that later checks it
must use the same one or the audit is checking the arithmetic rather than the
board. So the controlled-impedance path (M11) uses IPC-2141 throughout, and the
older ``estimate_gap`` path keeps Hammerstad so that no board built before M11
changes its geometry.

**Coplanar ground** (:func:`coplanar_odd_factor`) is M13b's addition and the one
with the longest reach. Both formulas above describe a *bare* microstrip -- a trace
over a plane with nothing beside it -- and every board this toolchain builds pours
ground up to its pairs at the class clearance. M12 measured what that costs:
`REFCLKP/N` came back at 50.9 ohm against the 85 ohm its width was derived for. A
trace with ground beside it as well as under it is a coplanar waveguide with
ground, and its impedance is lower.

The model that ships is IPC-2141's *own* edge-coupling term applied once per
neighbouring conductor and superposed as partial capacitances -- the pair's other
half on one side, the pour on the other. That is Ghione & Naldi's decomposition
(IEEE Trans. MTT-35, 1987, section II), which is what conformal-mapping treatments
of coplanar lines are built on, applied to the closed form this project already
derives and audits with. It reduces to exactly today's answer with one neighbour,
so no board without a pour beside its pairs moves at all.

**The published conductor-backed-CPW conformal map is here too**
(:func:`grounded_cpw`), because it is what M13b measured against and the number
belongs with the code rather than only in an ADR. It is not what ships, and the
reason is measurement rather than taste. Across the twelve simulated links M12
left behind, mean error against the solver is:

===================================================  ==========  ==========
model                                                  mean       RMS
===================================================  ==========  ==========
bare microstrip (what M11 shipped)                     +34.8 %     39.6 %
Wadell/Simons conductor-backed CPW, odd mode           +61.7 %     66.0 %
the same, taken as a ratio against its own far limit   +17.5 %     23.9 %
partial capacitances, which is what ships               +7.0 %     16.3 %
===================================================  ==========  ==========

The conformal form's trouble is its own far limit: as the coplanar gap opens, its
effective permittivity goes to ``er`` rather than to a microstrip's, so it reads
110 ohm where IPC-2141 reads 90 for the same isolated trace. That bias is fine
inside its domain (a genuine CPW, where the coplanar gap is comparable to the
substrate) and is not fine on the geometries this tool derives, where the backing
plane dominates. Dividing it out helps and does not fix it.

Everything here is pure arithmetic: no model imports, no I/O, no state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "DEFAULT_EPSILON_R",
    "FAR_GAP_MM",
    "DiffGeometry",
    "ImpedanceUnreachable",
    "coplanar_factor",
    "coplanar_odd_factor",
    "coupling_factor",
    "cpwg_differential",
    "cpwg_microstrip",
    "differential_impedance",
    "elliptic_ratio",
    "grounded_cpw",
    "hammerstad_microstrip",
    "ipc2141_microstrip",
    "neighbour_load",
    "solve_width",
]

#: What FR-4 is worth when the source does not say. The same number
#: ``compile/board.py`` writes into the ``.kicad_pcb`` stackup, deliberately: the
#: impedance this tool computes and the impedance KiCad reports should come from
#: one board, not two.
DEFAULT_EPSILON_R = 4.5

#: Bisection bounds for the width solver, in millimetres. Wide enough for any
#: microstrip anybody would etch, narrow enough that 200 halvings are exact.
_WIDTH_MIN = 0.02
_WIDTH_MAX = 5.0


class ImpedanceUnreachable(ValueError):
    """The impedance target cannot be met by the geometry on offer."""

    def __init__(self, target: float, low: float, high: float) -> None:
        super().__init__(
            f"{target:.0f} ohm differential is outside the {low:.0f}-{high:.0f} ohm "
            "range this stackup can reach"
        )
        self.target = target
        self.low = low
        self.high = high


@dataclass(frozen=True, slots=True)
class DiffGeometry:
    """A differential microstrip, and what it comes out at."""

    width_mm: float
    gap_mm: float
    height_mm: float
    """Dielectric between the trace and its reference plane."""
    epsilon_r: float
    copper_mm: float
    impedance_ohm: float
    pour_gap_mm: float | None = None
    """How far ground is poured from each side, or ``None`` for a bare microstrip.

    This is the one field that says *which model* the width came out of, so it is
    carried on the geometry rather than recomputed by whoever needs to know.
    """

    @property
    def model(self) -> str:
        return "microstrip" if self.pour_gap_mm is None else "cpwg"

    @property
    def pitch_mm(self) -> float:
        return self.width_mm + self.gap_mm


def hammerstad_microstrip(width: float, height: float, epsilon_r: float = 4.3) -> float:
    """Hammerstad's approximation for a single microstrip, copper thickness ignored."""
    effective = (epsilon_r + 1) / 2 + (epsilon_r - 1) / 2 * (
        1 / math.sqrt(1 + 12 * height / max(width, 1e-6))
    )
    ratio = width / max(height, 1e-6)
    if ratio < 1:
        return (60 / math.sqrt(effective)) * math.log(8 / ratio + ratio / 4)
    return (120 * math.pi / math.sqrt(effective)) / (
        ratio + 1.393 + 0.667 * math.log(ratio + 1.444)
    )


def ipc2141_microstrip(
    width: float, height: float, copper: float, epsilon_r: float
) -> float:
    """IPC-2141's surface-microstrip approximation.

        Z0 = 87 / sqrt(er + 1.41) * ln(5.98 h / (0.8 w + t))

    Valid roughly for ``0.1 <= w/h <= 3.0`` and ``1 <= er <= 15``, which the
    geometries this tool derives sit inside. Outside that band the number is still
    returned -- clamping it would hide the fact that the stackup is unusual -- but
    the caller should not believe it to a percent.
    """
    denominator = 0.8 * max(width, 1e-6) + max(copper, 0.0)
    inner = 5.98 * max(height, 1e-6) / max(denominator, 1e-6)
    if inner <= 1.0:
        # ln() of anything <= 1 is a non-positive impedance, which is not an
        # answer. It means the trace is wider than the formula's domain allows.
        return 0.0
    return (87.0 / math.sqrt(epsilon_r + 1.41)) * math.log(inner)


def coupling_factor(gap: float, height: float) -> float:
    """How much two edge-coupled microstrips pull each other's impedance down.

        Zdiff = 2 * Z0 * (1 - 0.48 * exp(-0.96 s / h))

    At ``s = 0`` the factor is 0.52; as the gap opens it goes to 1 and the pair
    stops being a pair. This is the standard closed form and the one both formulas
    above are paired with.
    """
    return 1.0 - 0.48 * math.exp(-0.96 * max(gap, 0.0) / max(height, 1e-6))


def differential_impedance(
    width: float, gap: float, height: float, copper: float, epsilon_r: float
) -> float:
    """Differential impedance of an edge-coupled microstrip pair, IPC-2141 based."""
    return (
        2.0
        * ipc2141_microstrip(width, height, copper, epsilon_r)
        * coupling_factor(gap, height)
    )


#: What "no coplanar ground beside this trace" is worth, in millimetres. Ten metres
#: of gap on a board measured in millimetres: the conformal map's coplanar term is
#: numerically dead long before this, and using one number rather than a limit keeps
#: the ratio a plain function of two evaluations of the same formula.
FAR_GAP_MM = 1e4


def elliptic_ratio(k: float) -> float:
    """``K(k) / K(k')``, where ``K`` is the complete elliptic integral of the first
    kind and ``k' = sqrt(1 - k^2)``.

    Computed from the arithmetic-geometric mean rather than from one of the usual
    closed-form approximations (Hilberg's, say). The AGM converges quadratically, so
    twenty iterations is exact to double precision everywhere in ``0 < k < 1`` --
    which means this function contributes no error of its own to a number that
    already carries plenty, and it is the same on any machine, which the byte
    stability policy needs.
    """
    k = min(max(k, 1e-12), 1.0 - 1e-12)

    def complete(modulus: float) -> float:
        a, b = 1.0, math.sqrt(max(0.0, 1.0 - modulus * modulus))
        for _ in range(60):
            if abs(a - b) < 1e-16:
                break
            a, b = (a + b) / 2, math.sqrt(a * b)
        return math.pi / (2 * a)

    return complete(k) / complete(math.sqrt(1.0 - k * k))


def grounded_cpw(width: float, gap: float, height: float, epsilon_r: float) -> float:
    """Conductor-backed coplanar waveguide, by conformal mapping.

        k1 = w / (w + 2 s)
        k2 = tanh(pi w / 4 h) / tanh(pi (w + 2 s) / 4 h)
        er_eff = (1 + er R) / (1 + R),  R = [K(k1')/K(k1)] [K(k2)/K(k2')]
        Z0 = 60 pi / ( sqrt(er_eff) [ K(k1)/K(k1') + K(k2)/K(k2') ] )

    The standard result: Ghione & Naldi, "Coplanar waveguides for MMIC
    applications", IEEE Trans. MTT-35 (1987); Wadell, *Transmission Line Design
    Handbook* (Artech House, 1991) section 3.3.2; Simons, *Coplanar Waveguide
    Circuits, Components and Systems* (Wiley, 2001) equations 3.29-3.31. Copper
    thickness is **not** modelled -- see this module's own docstring for the
    measurement that decided that, and note that the thickness term cancels in the
    ratio this is actually used through.

    Kept for the record rather than used: this module's own docstring has the
    measurement that chose against it. Two numbers from that measurement, so the
    claim is checkable here as well as there. On the standard 50 ohm
    conductor-backed CPW design -- ``w=1.0, s=0.25, h=0.508, er=3.48`` on
    RO4350 -- this returns **48.4 ohm**, within 3 % of the intended 50, which is
    the model working inside its domain. On `examples/pcie-sata`'s 85 ohm class
    geometry it returns **109.6 ohm** for an isolated trace where IPC-2141 returns
    89.6: as the coplanar gap opens, ``er_eff`` here tends to ``er`` rather than to
    a microstrip's, and on a geometry whose backing plane dominates that bias is
    larger than the coplanar effect being measured.
    """
    width = max(width, 1e-9)
    gap = max(gap, 1e-9)
    height = max(height, 1e-9)
    k1 = width / (width + 2 * gap)
    k2 = math.tanh(math.pi * width / (4 * height)) / math.tanh(
        math.pi * (width + 2 * gap) / (4 * height)
    )
    r1 = 1.0 / elliptic_ratio(k1)
    r2 = elliptic_ratio(k2)
    effective = (1 + epsilon_r * r1 * r2) / (1 + r1 * r2)
    return (60 * math.pi / math.sqrt(effective)) / (1.0 / r1 + r2)


def neighbour_load(gap: float, height: float) -> float:
    """How much capacitance one conductor at ``gap`` adds beside a trace, relative
    to the trace's own capacitance to its reference plane.

    This is :func:`coupling_factor` turned inside out. That function returns
    ``1 - c`` where ``c = 0.48 exp(-0.96 s/h)``, and it is the ratio of an
    odd-mode impedance to the isolated one -- which, since ``Z`` goes as ``1/C``,
    means the neighbour added ``x = c / (1 - c)`` times the trace's own
    capacitance. Written that way it *superposes*: two neighbours add ``x1 + x2``,
    which is exactly the partial-capacitance decomposition that conformal-mapping
    treatments of coplanar lines are built on (Ghione & Naldi 1987, section II;
    Wadell section 3.3).

    One neighbour reproduces IPC-2141 exactly, by construction, so nothing that
    used the old formula moves.
    """
    coupling = 0.48 * math.exp(-0.96 * max(gap, 0.0) / max(height, 1e-6))
    return coupling / max(1.0 - coupling, 1e-9)


def coplanar_factor(gap: float, height: float) -> float:
    """What ground poured on *both* sides at ``gap`` does to a single trace.

    Two neighbours, so twice the load. Goes to 1 as the pour recedes, which is
    what keeps a board with no pour beside its traces deriving exactly the
    geometry it derived before M13.
    """
    return 1.0 / (1.0 + 2.0 * neighbour_load(gap, height))


def coplanar_odd_factor(pair_gap: float, pour_gap: float, height: float) -> float:
    """What ground poured alongside does to a *pair*, as a fraction of its bare
    differential impedance.

    Under differential excitation each trace already has one neighbour -- the
    other half of the pair, at ``pair_gap`` -- and the baseline
    :func:`differential_impedance` is exactly that one-neighbour case. The pour
    adds a second, on the outside, at ``pour_gap``. What is returned is the ratio
    between the two, so it composes with a baseline this project has used and
    audited since M11 rather than replacing it.
    """
    mate = neighbour_load(pair_gap, height)
    pour = neighbour_load(pour_gap, height)
    return (1.0 + mate) / (1.0 + mate + pour)


def cpwg_microstrip(
    width: float, gap: float, height: float, copper: float, epsilon_r: float
) -> float:
    """Single-ended impedance of a trace with ground beside it as well as under it."""
    return ipc2141_microstrip(width, height, copper, epsilon_r) * coplanar_factor(
        gap, height
    )


def cpwg_differential(
    width: float,
    gap: float,
    height: float,
    copper: float,
    epsilon_r: float,
    pour_gap: float,
) -> float:
    """Differential impedance of an edge-coupled pair with ground poured alongside."""
    return differential_impedance(
        width, gap, height, copper, epsilon_r
    ) * coplanar_odd_factor(gap, pour_gap, height)


def solve_width(
    target_ohm: float,
    gap: float,
    height: float,
    copper: float,
    epsilon_r: float,
    pour_gap: float | None = None,
) -> DiffGeometry:
    """The trace width that hits ``target_ohm`` differential at a given gap.

    ``pour_gap`` is how far ground will be poured from each side of the pair, or
    ``None`` when nothing will be. It scales the impedance by a constant -- the
    coplanar factor depends on the gaps and the dielectric, not on the width -- so
    the bisection below stays monotone and every word of what follows still holds.

    Impedance falls monotonically as the trace widens, so bisection is exact and
    deterministic -- no seed, no tolerance the answer depends on. Two hundred
    halvings of a 0.02-5 mm bracket is well past double precision, so the result is
    a function of the inputs alone and is safe to compare byte-for-byte between
    runs.

    Raises :class:`ImpedanceUnreachable` when the target lies outside what the
    bracket can produce, which is a real answer: the stackup or the gap has to
    change, and silently clamping to the nearest width would hide it.
    """
    # IPC-2141's logarithm runs out of domain when the trace gets wide enough that
    # `0.8 w + t` reaches `5.98 h`, and past that the formula returns zero rather
    # than an impedance. Bracketing there instead of at an arbitrary five
    # millimetres keeps the bisection between two *real* answers -- without it the
    # bracket contains a zero and every target below the true minimum comes back
    # "reachable" at the widest trace the bracket holds.
    def impedance(width: float) -> float:
        if pour_gap is None:
            return differential_impedance(width, gap, height, copper, epsilon_r)
        return cpwg_differential(width, gap, height, copper, epsilon_r, pour_gap)

    widest_valid = min(_WIDTH_MAX, 0.98 * (5.98 * max(height, 1e-6) - copper) / 0.8)
    if widest_valid <= _WIDTH_MIN:
        raise ImpedanceUnreachable(target_ohm, 0.0, 0.0)
    widest = impedance(widest_valid)
    narrowest = impedance(_WIDTH_MIN)
    if not widest <= target_ohm <= narrowest:
        raise ImpedanceUnreachable(target_ohm, widest, narrowest)

    low, high = _WIDTH_MIN, widest_valid
    for _ in range(200):
        middle = (low + high) / 2
        if impedance(middle) > target_ohm:
            low = middle
        else:
            high = middle
    width = round((low + high) / 2, 4)
    return DiffGeometry(
        width_mm=width,
        gap_mm=round(gap, 4),
        height_mm=round(height, 4),
        epsilon_r=epsilon_r,
        copper_mm=copper,
        impedance_ohm=round(impedance(width), 2),
        pour_gap_mm=None if pour_gap is None else round(pour_gap, 4),
    )

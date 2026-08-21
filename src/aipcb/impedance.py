"""Closed-form impedance approximations, in one place.

Two formulas live here, and the difference between them matters enough to be
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

Everything here is pure arithmetic: no model imports, no I/O, no state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "DEFAULT_EPSILON_R",
    "DiffGeometry",
    "ImpedanceUnreachable",
    "coupling_factor",
    "differential_impedance",
    "hammerstad_microstrip",
    "ipc2141_microstrip",
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


def solve_width(
    target_ohm: float,
    gap: float,
    height: float,
    copper: float,
    epsilon_r: float,
) -> DiffGeometry:
    """The trace width that hits ``target_ohm`` differential at a given gap.

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
    widest_valid = min(_WIDTH_MAX, 0.98 * (5.98 * max(height, 1e-6) - copper) / 0.8)
    if widest_valid <= _WIDTH_MIN:
        raise ImpedanceUnreachable(target_ohm, 0.0, 0.0)
    widest = differential_impedance(widest_valid, gap, height, copper, epsilon_r)
    narrowest = differential_impedance(_WIDTH_MIN, gap, height, copper, epsilon_r)
    if not widest <= target_ohm <= narrowest:
        raise ImpedanceUnreachable(target_ohm, widest, narrowest)

    low, high = _WIDTH_MIN, widest_valid
    for _ in range(200):
        middle = (low + high) / 2
        if differential_impedance(middle, gap, height, copper, epsilon_r) > target_ohm:
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
        impedance_ohm=round(
            differential_impedance(width, gap, height, copper, epsilon_r), 2
        ),
    )

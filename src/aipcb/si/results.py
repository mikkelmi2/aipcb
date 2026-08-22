"""From S-parameters to a verdict, in the same shape as every other check.

openEMS hands back two CSV files -- one per excitation -- holding the four-port
S-matrix column by column. Everything a designer wants is a combination of those
columns: the differential impedance the pair actually has, how much of the signal
comes back, how much gets through. This module does that arithmetic, writes the raw
matrix out as Touchstone for anyone who would rather use scikit-rf, and turns the
numbers into findings that point at the source lines that produced them.

**Thresholds here are engineering defaults, not standards compliance.** Impedance
within ten percent and return loss better than -10 dB are the numbers most
controlled-impedance fabrication notes use, and they are what the settings default
to; a link with a real budget should state its own. Nothing in this file certifies a
design against PCIe, SATA or anything else.

**And the numbers describe the layout, not the board.** They come from the stackup
the source declares. A fabricator who presses different material builds a different
impedance. This finds a pair routed too narrow; it does not replace a coupon.
"""

from __future__ import annotations

import cmath
import csv
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.model.simulation import ResolvedSimulation

__all__ = [
    "MAX_FIT_PS",
    "SKEW_FIT_FLOOR_DB",
    "SKEW_FIT_IMPROVEMENT_DB",
    "Metrics",
    "SParameters",
    "SkewFit",
    "analyse",
    "fit_skew",
    "read_sparameters",
    "write_touchstone",
]

#: Below this the FDTD run has very little energy in the excitation, so the ratio
#: that makes an S-parameter is a small number divided by a small number. Phase 0
#: saw |S| slightly above 1 down here on an otherwise sound run. The band a verdict
#: is read from therefore starts above it.
_NOISE_FLOOR_HZ = 5e8

#: |Sdd21| above one is energy the simulation did not put in, and it is the marker
#: phase 0 used to call an extraction broken. The threshold is calibrated rather than
#: chosen: phase 0's coarse, non-converging diff-pair slice peaks at **1.166**, while
#: M12's `mcu-4layer` runs -- whose impedance agrees to 1.2% across a 4.5x change in
#: mesh density, so they *are* converged -- peak at 1.07 and 1.09. The gap between
#: those is where this sits. Under it, the excess is the truncation error of a run
#: that stopped with energy still bouncing around the board's planes; over it, the
#: numbers mean nothing.
_GAIN_UNUSABLE = 1.15


@dataclass(slots=True)
class SParameters:
    """A port-count square S-matrix over frequency, as far as it was measured."""

    frequencies: list[float]
    excited: tuple[int, ...]
    ports: int
    values: dict[tuple[int, int], list[complex]]
    """``(out, in)`` to the column over frequency. Only excited inputs are present."""

    def s(self, out: int, into: int) -> list[complex]:
        return self.values[(out, into)]

    def at(self, hz: float) -> int:
        """Index of the sample nearest ``hz``."""
        return min(
            range(len(self.frequencies)), key=lambda i: abs(self.frequencies[i] - hz)
        )


def read_sparameters(simulation_dir: Path, ports: int = 4) -> SParameters:
    """Read gerber2ems's ``Sx<n>.csv`` files back into an S-matrix.

    The header names each column ``re(S<out>-<in>)`` / ``im(S<out>-<in>)``, and the
    rows carry a trailing comma, so the parser reads the header rather than assuming
    a column order.
    """
    frequencies: list[float] = []
    values: dict[tuple[int, int], list[complex]] = {}
    excited: list[int] = []
    for path in sorted(simulation_dir.glob("Sx*.csv")):
        try:
            into = int(path.stem[2:])
        except ValueError:
            continue
        rows = list(csv.reader(path.open(encoding="utf-8")))
        if len(rows) < 2:
            continue
        header = [cell.strip() for cell in rows[0] if cell.strip()]
        real: dict[int, list[float]] = {}
        imag: dict[int, list[float]] = {}
        local: list[float] = []
        for row in rows[1:]:
            cells = [float(cell) for cell in row if cell.strip()]
            if len(cells) != len(header):
                continue
            local.append(cells[0] * 1e6)
            for index, name in enumerate(header[1:], start=1):
                if name.startswith("re(S"):
                    out = int(name[4:].split("-")[0])
                    real.setdefault(out, []).append(cells[index])
                elif name.startswith("im(S"):
                    out = int(name[4:].split("-")[0])
                    imag.setdefault(out, []).append(cells[index])
        if not local:
            continue
        frequencies = local
        excited.append(into)
        for out in sorted(real):
            values[(out, into)] = [
                complex(a, b) for a, b in zip(real[out], imag.get(out, []), strict=False)
            ]
    return SParameters(
        frequencies=frequencies, excited=tuple(sorted(excited)), ports=ports, values=values
    )


def _db(value: complex) -> float:
    magnitude = abs(value)
    return 20 * math.log10(magnitude) if magnitude > 1e-30 else -300.0


@dataclass(slots=True)
class Metrics:
    """Everything one pair's simulation says, with the verdicts already taken."""

    pair: str
    net_class: str
    target_ohm: float | None
    impedance_ohm: float
    impedance_min_ohm: float
    impedance_max_ohm: float
    band_hz: tuple[float, float]
    worst_return_loss_db: float
    worst_return_loss_hz: float
    insertion_loss_db: dict[str, float]
    """Differential insertion loss at the class's key frequencies, keyed by label."""
    delay_ns: float | None
    conductor_mm: float = 0.0
    """Total conductor in the slice, both halves, as the slicer measured it."""
    mode_conversion_db: float = -300.0
    """Worst differential-to-common conversion in the band. Skew is what makes it."""
    mode_conversion_hz: float = 0.0
    skew_fit: SkewFit | None = None
    """The frequency-domain read of the same data (M13c). Where the verdict lives."""
    geometric_skew_mm: float | None = None
    """What M11e measured on the copper, for the two layers to be compared."""
    slice_skew_mm: float | None = None
    """How far out of length the *sliced* structure is, which need not be the same.

    The slicer bridges capacitors, clips to an outline and grows a launch at each
    of the four ends. What the solver sees is therefore not quite what the router
    drew, and a fitted delay is a measurement of what the solver saw. Carrying this
    beside the other two is what turns "the fit and the router disagree" into a
    number with a candidate explanation attached.
    """
    max_skew_mm: float | None = None
    """The class's budget, so the fit has something to be a verdict against."""
    usable: bool = True
    """False when the extraction is not physical, whatever the numbers say."""
    verdicts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    """Caveats a reader needs, in the order they were found. Plain prose."""

    @property
    def deviation(self) -> float | None:
        if not self.target_ohm:
            return None
        return (self.impedance_ohm - self.target_ohm) / self.target_ohm

    @property
    def ps_per_mm(self) -> float | None:
        """The pair's own measured propagation, for turning a delay into a length.

        Measured rather than assumed: the group delay comes from this pair's
        transmission phase and the conductor length from its own slice, so the
        conversion between the two verification layers uses no constant either of
        them had to agree on beforehand.

        ``conductor_mm`` counts *both* halves, because that is what the slicer
        measures; the signal travels one of them, so the delay is over half of it.
        """
        if self.delay_ns is None or self.conductor_mm <= 0:
            return None
        return self.delay_ns * 1000.0 / (self.conductor_mm / 2)

    @property
    def fitted_skew_mm(self) -> float | None:
        """The fitted delay expressed as a length mismatch, or ``None``.

        This is the number the class's `max_skew_mm` budget is written in, so it
        is the one the verdict is taken on.
        """
        if self.skew_fit is None:
            return None
        rate = self.ps_per_mm
        if not rate:
            return None
        return self.skew_fit.delay_ps / rate

    def to_dict(self) -> dict[str, object]:
        return {
            "pair": self.pair,
            "net_class": self.net_class,
            "target_ohm": self.target_ohm,
            "impedance_ohm": round(self.impedance_ohm, 2),
            "impedance_min_ohm": round(self.impedance_min_ohm, 2),
            "impedance_max_ohm": round(self.impedance_max_ohm, 2),
            "deviation": None if self.deviation is None else round(self.deviation, 4),
            "band_ghz": [round(f / 1e9, 3) for f in self.band_hz],
            "worst_return_loss_db": round(self.worst_return_loss_db, 2),
            "worst_return_loss_ghz": round(self.worst_return_loss_hz / 1e9, 3),
            "insertion_loss_db": {
                k: round(v, 2) for k, v in sorted(self.insertion_loss_db.items())
            },
            "delay_ns": None if self.delay_ns is None else round(self.delay_ns, 4),
            "mode_conversion_db": round(self.mode_conversion_db, 2),
            "mode_conversion_ghz": round(self.mode_conversion_hz / 1e9, 3),
            "skew_fit": None if self.skew_fit is None else self.skew_fit.to_dict(),
            "fitted_skew_mm": (
                None if self.fitted_skew_mm is None else round(self.fitted_skew_mm, 4)
            ),
            "geometric_skew_mm": (
                None
                if self.geometric_skew_mm is None
                else round(self.geometric_skew_mm, 4)
            ),
            "slice_skew_mm": (
                None if self.slice_skew_mm is None else round(self.slice_skew_mm, 4)
            ),
            "max_skew_mm": self.max_skew_mm,
            "ps_per_mm": None if self.ps_per_mm is None else round(self.ps_per_mm, 3),
            "usable": self.usable,
            "verdicts": dict(sorted(self.verdicts.items())),
            "notes": list(self.notes),
        }


def _mixed_mode(
    sp: SParameters,
) -> tuple[list[complex], list[complex], list[complex], list[complex]] | None:
    """``(gamma_dd, Sdd11, Sdd21, Scd21)`` from the four-port matrix, or ``None``.

    ``gamma_dd`` is gerber2ems's own differential reflection coefficient -- the one
    its ``Z_diff`` plot uses -- reproduced here so the number this tool reports and
    the picture the solver draws are the same number. Ports are ordered by the slice
    generator as P-start, P-stop, N-start, N-stop, so the driven end is 0 and 2.
    """
    needed = [(0, 0), (2, 0), (0, 2), (2, 2), (1, 0), (1, 2), (3, 0), (3, 2)]
    if any(key not in sp.values for key in needed):
        return None
    gamma: list[complex] = []
    sdd11: list[complex] = []
    sdd21: list[complex] = []
    scd21: list[complex] = []
    for index in range(len(sp.frequencies)):
        s11 = sp.s(0, 0)[index]
        s21 = sp.s(2, 0)[index]
        s12 = sp.s(0, 2)[index]
        s22 = sp.s(2, 2)[index]
        denominator = (2 - s21) * (1 - s22 - s12) + (1 - s11 - s21) * (1 + s22)
        numerator = (2 * s11 - s21) * (1 - s22 - s12) + (1 - s11 - s21) * (
            1 + s22 - 2 * s12
        )
        gamma.append(numerator / denominator if denominator != 0 else complex(1, 0))
        sdd11.append(0.5 * (s11 - s21 - s12 + s22))
        sdd21.append(
            0.5
            * (
                sp.s(1, 0)[index]
                - sp.s(1, 2)[index]
                - sp.s(3, 0)[index]
                + sp.s(3, 2)[index]
            )
        )
        # Differential in, common out: the far end's mode conversion. Length
        # mismatch inside a pair is what produces it, so this is the metric that
        # sees skew where an impedance number cannot.
        scd21.append(
            0.5
            * (
                sp.s(1, 0)[index]
                - sp.s(1, 2)[index]
                + sp.s(3, 0)[index]
                - sp.s(3, 2)[index]
            )
        )
    return gamma, sdd11, sdd21, scd21


#: The longest skew the frequency-domain fit will look for, in picoseconds. Beyond
#: ``1 / (2 f_max)`` the ``sin(pi f dt)`` model has already turned over inside the
#: band and a longer delay is indistinguishable from a shorter one -- so the fit is
#: bounded at the point where it would stop being a measurement. At an 8 GHz stop
#: that is 62.5 ps, which is thirty times the largest skew any board here carries.
MAX_FIT_PS = 62.5

#: Coarse and fine grid steps for the fit, in picoseconds. A grid rather than a
#: solver: the objective is not convex in ``dt``, a seeded optimiser would make the
#: answer depend on the seed, and the whole search costs a few tens of thousands of
#: multiplications.
_COARSE_PS = 0.25
_FINE_PS = 0.005

#: How far above the fitted floor the skew term has to reach, in dB, before the
#: fitted delay is called a measurement rather than an upper bound. Below this the
#: conversion the skew produces is buried in everything else the board converts, and
#: M12 measured that floor varying by more than 25 dB between pairs on one board --
#: so "the fit says 0.4 ps" has to mean "no more than 0.4 ps", not "0.4 ps".
SKEW_FIT_FLOOR_DB = 3.0

#: How much better the two-term fit has to be than a flat floor alone, in dB of
#: sum-of-squares, before the fitted delay is believed. Set from the measured
#: noise: see `docs/reports/m13.md` for the per-link table this came out of.
SKEW_FIT_IMPROVEMENT_DB = 3.0


@dataclass(slots=True)
class SkewFit:
    """What the mode-conversion curve says the pair's intra-pair delay is.

    M12 asked whether simulation can see the skew M11 delivered over budget, and
    the answer through the published scalar was no -- worse than no, since the
    three links it flagged were the three best-matched ones. The reason was not
    that the physics is invisible; it is that a *worst-in-band scalar* reads a
    floor, and the floor on that board ran from -29 dB to -14 dB while the skew
    under test moved the number by about 3 dB.

    Read across frequency the two separate cleanly, because they have different
    shapes. Skew converts as ``|sin(pi f dt)|`` -- zero at DC, rising 6 dB per
    octave -- while the floor (launch asymmetry, via barrels, the pour's asymmetry
    around each trace, truncation noise) is broadly flat. Fitting the two-term
    model ``|Scd21|^2 = |Sdd21|^2 sin^2(pi f dt) + floor^2`` therefore recovers
    ``dt`` from the *slope* rather than the level, which is the part the floor
    cannot fake.
    """

    delay_ps: float
    """The fitted intra-pair delay."""
    floor_db: float
    """The flat term the fit had to add underneath, as 10 log10 of its power."""
    residual_db: float
    """RMS distance between the fitted curve and the measurement, in dB."""
    peak_over_floor_db: float
    """How far the fitted skew term rises above the fitted floor inside the band."""
    improvement_db: float
    """How much better the two-term fit is than a flat floor with no skew at all.

    The one number that says whether the *shape* is there. A pair whose conversion
    is flat gets no improvement from a ``sin^2`` term however large a delay the
    scan picks, and this is what separates "the fit measured 2 ps" from "the fit
    had to put 2 ps somewhere".
    """
    points: int

    @property
    def confident(self) -> bool:
        """Whether the delay is a measurement rather than an upper bound.

        Both tests, because they fail in different ways. The improvement test
        catches a flat curve the scan has fitted anyway; the peak-over-floor test
        catches a curve with the right shape whose skew term is still small
        against everything else the board converts.
        """
        return (
            self.improvement_db >= SKEW_FIT_IMPROVEMENT_DB
            and self.peak_over_floor_db >= SKEW_FIT_FLOOR_DB
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "delay_ps": round(self.delay_ps, 4),
            "floor_db": round(self.floor_db, 2),
            "residual_db": round(self.residual_db, 2),
            "peak_over_floor_db": round(self.peak_over_floor_db, 2),
            "improvement_db": round(self.improvement_db, 2),
            "points": self.points,
            "confident": self.confident,
        }


def fit_skew(
    frequencies: list[float],
    sdd21: list[complex],
    scd21: list[complex],
    band: list[int],
) -> SkewFit | None:
    """Fit ``|Scd21|^2 = |Sdd21|^2 sin^2(pi f dt) + floor^2`` over the band.

    Two parameters, and only one of them is searched. For any candidate ``dt`` the
    best flat floor is the mean of what the skew term does not account for, in
    power -- a closed form, not a search -- so what is left is a one-dimensional
    scan over ``dt``. Coarse then fine, on a fixed grid, so the answer is a
    function of the data alone: no seed, no tolerance, and the same on any machine.

    A measured amplitude rather than a fitted one: how much energy arrives at the
    far end is data, and letting the fit choose it lets a lossy link and a big skew
    trade against each other.
    """
    if len(band) < 8:
        return None
    measured = [abs(scd21[i]) ** 2 for i in band]
    # The amplitude is the total power that got through, differential *and*
    # converted. A pair with skew dt transmits cos(pi f dt) differentially and
    # converts sin(pi f dt), so the two together are what the line delivered and
    # what the sin^2 term is a fraction of. Using |Sdd21| alone would make a link
    # that converts a lot look like one that lost a lot, and the fit would buy the
    # difference back as extra delay.
    through = [abs(sdd21[i]) ** 2 + abs(scd21[i]) ** 2 for i in band]
    hz = [frequencies[i] for i in band]

    def residual(delay_s: float) -> tuple[float, float]:
        """``(sum of squares, floor power)`` for one candidate delay."""
        skewed = [
            t * math.sin(math.pi * f * delay_s) ** 2 for f, t in zip(hz, through, strict=True)
        ]
        floor = sum(m - s for m, s in zip(measured, skewed, strict=True)) / len(measured)
        floor = max(floor, 0.0)
        total = sum(
            (m - s - floor) ** 2 for m, s in zip(measured, skewed, strict=True)
        )
        return total, floor

    def scan(low: float, high: float, step: float) -> float:
        best_delay, best_total = low, math.inf
        steps = max(round((high - low) / step), 1)
        for index in range(steps + 1):
            delay = low + index * step
            total, _ = residual(delay * 1e-12)
            if total < best_total:
                best_total, best_delay = total, delay
        return best_delay

    coarse = scan(0.0, MAX_FIT_PS, _COARSE_PS)
    delay_ps = scan(
        max(0.0, coarse - _COARSE_PS), min(MAX_FIT_PS, coarse + _COARSE_PS), _FINE_PS
    )
    best, floor = residual(delay_ps * 1e-12)
    flat, _ = residual(0.0)

    fitted = [
        t * math.sin(math.pi * f * delay_ps * 1e-12) ** 2 + floor
        for f, t in zip(hz, through, strict=True)
    ]
    errors = [
        abs(_db_power(m) - _db_power(v)) for m, v in zip(measured, fitted, strict=True)
    ]
    rms = math.sqrt(sum(e * e for e in errors) / len(errors))
    peak = max(
        t * math.sin(math.pi * f * delay_ps * 1e-12) ** 2
        for f, t in zip(hz, through, strict=True)
    )
    # A floor the fit drove to zero is not an infinitely quiet board; it is a fit
    # that had nothing flat left to explain. Bounding it by the smallest measured
    # conversion keeps `peak_over_floor` a number rather than an artefact.
    floor = max(floor, min(measured))
    return SkewFit(
        delay_ps=delay_ps,
        floor_db=_db_power(floor),
        residual_db=rms,
        peak_over_floor_db=_db_power(peak) - _db_power(floor),
        improvement_db=_db_power(flat) - _db_power(best) if best > 0 else 0.0,
        points=len(band),
    )


def _db_power(value: float) -> float:
    return 10 * math.log10(value) if value > 1e-30 else -300.0


def analyse(
    sp: SParameters,
    *,
    pair: str,
    net_class: str,
    port_impedance_ohm: float,
    target_ohm: float | None,
    settings: ResolvedSimulation,
    length_mm: float | None = None,
    geometric_skew_mm: float | None = None,
    max_skew_mm: float | None = None,
    slice_skew_mm: float | None = None,
) -> Metrics | None:
    """Turn one pair's S-matrix into numbers and verdicts."""
    mixed = _mixed_mode(sp)
    if mixed is None or not sp.frequencies:
        return None
    gamma, sdd11, sdd21, scd21 = mixed

    low = max(_NOISE_FLOOR_HZ, settings.start_hz)
    high = settings.stop_hz
    wide = [i for i, f in enumerate(sp.frequencies) if low <= f <= high]
    if not wide:
        wide = list(range(len(sp.frequencies)))

    notes: list[str] = []
    delay = _group_delay(sp.frequencies, sdd21, wide)
    band, whole_periods = _impedance_band(sp.frequencies, wide, delay)
    if not whole_periods:
        notes.append(
            "the swept band is shorter than one standing-wave period on this pair, "
            "so the impedance below is one point of the ripple rather than its centre"
        )

    impedances: list[float] = []
    for index in band:
        g = gamma[index]
        if abs(1 - g) < 1e-12:
            continue
        impedances.append(abs(port_impedance_ohm * (1 + g) / (1 - g)))
    if not impedances:
        return None

    # The median, not the value at one frequency. A line whose impedance differs
    # from the port's rings: the input impedance swings above and below the true
    # characteristic impedance with a period set by the line's length. Reading one
    # frequency reads one point of that swing; the median over a band that spans it
    # is the line itself. The spread is reported alongside so a reader can see how
    # much swing was there rather than trusting a single number.
    impedance = statistics.median(impedances)
    peak = max(abs(sdd21[i]) for i in wide)
    usable = peak <= _GAIN_UNUSABLE
    if not usable:
        notes.append(
            f"|Sdd21| reaches {peak:.2f}, which is more energy than went in; the "
            "extraction is not physical and the numbers below are not usable"
        )
    elif peak > 1.0:
        notes.append(
            f"|Sdd21| reaches {peak:.3f}, marginally above unity. That is truncation "
            "noise rather than a broken extraction, but it bounds the accuracy of "
            "everything else here at a few percent"
        )

    worst_rl_index = max(wide, key=lambda i: abs(sdd11[i]))
    worst_cd_index = max(wide, key=lambda i: abs(scd21[i]))
    key_points = {
        f"{settings.stop_hz / 2e9:.1f}GHz": sp.at(settings.stop_hz / 2),
        f"{settings.stop_hz / 1e9:.1f}GHz": sp.at(settings.stop_hz),
    }
    insertion = {label: _db(sdd21[index]) for label, index in key_points.items()}

    if length_mm:
        # A pair on FR-4 carries 3-8 ps per millimetre depending on how much of the
        # field is in the laminate. A delay far outside that for the length that was
        # sliced means the ports are measuring something other than the trace -- the
        # cheapest available check that the geometry and the excitation agree.
        one_way = length_mm / 2
        if delay is not None and not (
            0.25 * 3e-3 * one_way <= delay <= 4 * 8e-3 * one_way
        ):
            notes.append(
                f"the measured group delay is {delay * 1000:.0f} ps over {one_way:.1f} "
                "mm of conductor, which is nothing like the 3-8 ps/mm this laminate "
                "carries; the ports are not measuring the trace"
            )

    metrics = Metrics(
        pair=pair,
        net_class=net_class,
        target_ohm=target_ohm,
        impedance_ohm=impedance,
        impedance_min_ohm=min(impedances),
        impedance_max_ohm=max(impedances),
        band_hz=(sp.frequencies[band[0]], sp.frequencies[band[-1]]),
        worst_return_loss_db=_db(sdd11[worst_rl_index]),
        worst_return_loss_hz=sp.frequencies[worst_rl_index],
        insertion_loss_db=insertion,
        delay_ns=delay,
        conductor_mm=length_mm or 0.0,
        mode_conversion_db=_db(scd21[worst_cd_index]),
        mode_conversion_hz=sp.frequencies[worst_cd_index],
        skew_fit=fit_skew(sp.frequencies, sdd21, scd21, wide),
        geometric_skew_mm=geometric_skew_mm,
        slice_skew_mm=slice_skew_mm,
        max_skew_mm=max_skew_mm,
        usable=usable,
        notes=notes,
    )
    _verdicts(metrics, settings)
    return metrics


def _impedance_band(
    frequencies: list[float], wide: list[int], delay_ns: float | None
) -> tuple[list[int], bool]:
    """Trim the band to a whole number of standing-wave periods.

    A line whose impedance differs from its termination rings: its input impedance
    swings above and below its own characteristic impedance with a period of
    ``1 / (2 * delay)``. Over a whole number of those periods the swing is symmetric
    in the logarithm, so the median lands on the line; over one and a half it lands
    wherever the half period happened to be. The delay is measured from the pair's
    own transmission phase, so the correction comes out of the data rather than out
    of an assumed propagation velocity.

    The count is *rounded*, not floored, and clamped to the band. Flooring was tried
    and measured worse: on the `mcu-4layer` convergence sweep a band holding 1.9
    periods got cut to 1, which threw away half the samples and spread three mesh
    densities over 6.5% where keeping all of them spread them over 5.3%. Rounding
    shortens the band only when that gets *closer* to a whole number of periods, and
    otherwise leaves it alone.
    """
    if delay_ns is None or delay_ns <= 0 or len(wide) < 16:
        return wide, False
    period = 1.0 / (2 * delay_ns * 1e-9)
    low, high = frequencies[wide[0]], frequencies[wide[-1]]
    count = round((high - low) / period)
    if count < 1:
        return wide, False
    top = min(low + count * period, high)
    return [i for i in wide if frequencies[i] <= top], True


def _group_delay(
    frequencies: list[float], sdd21: list[complex], band: list[int]
) -> float | None:
    """Mean group delay across the band, from the unwrapped phase of Sdd21.

    Useful as a sanity check more than as a result: a differential pair on FR-4
    carries about 6 ps per millimetre, so a delay wildly away from the trace length
    means the port is not measuring the trace.
    """
    if len(band) < 8:
        return None
    phases: list[float] = []
    previous = 0.0
    offset = 0.0
    for index in band:
        angle = cmath.phase(sdd21[index])
        if phases:
            while angle + offset - previous > math.pi:
                offset -= 2 * math.pi
            while angle + offset - previous < -math.pi:
                offset += 2 * math.pi
        previous = angle + offset
        phases.append(previous)
    span = frequencies[band[-1]] - frequencies[band[0]]
    if span <= 0:
        return None
    slope = (phases[-1] - phases[0]) / span
    return -slope / (2 * math.pi) * 1e9


def _verdicts(metrics: Metrics, settings: ResolvedSimulation) -> None:
    if not metrics.usable:
        metrics.verdicts["impedance"] = "unusable"
    elif metrics.target_ohm and metrics.deviation is not None:
        metrics.verdicts["impedance"] = (
            "pass" if abs(metrics.deviation) <= settings.impedance_tolerance else "warn"
        )
    else:
        metrics.verdicts["impedance"] = "no-target"
    metrics.verdicts["return_loss"] = (
        "pass"
        if metrics.worst_return_loss_db <= settings.return_loss_db
        else "warn"
    )
    worst = min(metrics.insertion_loss_db.values(), default=0.0)
    metrics.verdicts["insertion_loss"] = (
        "pass" if worst >= settings.insertion_loss_db else "warn"
    )
    # Stays a warn, and stays labelled. M12 measured this scalar picking the wrong
    # three pairs out of eleven -- the three best-matched ones -- because what a
    # worst-in-band maximum reads on these boards is the mode-conversion *floor*,
    # not the skew. It is kept because it is a real, reproducible measurement of
    # how much differential energy leaves as common mode; what it is not is a skew
    # verdict, and until M13c nothing in the output said so.
    metrics.verdicts["mode_conversion"] = (
        "pass"
        if metrics.mode_conversion_db <= settings.mode_conversion_db
        else "warn-low-confidence"
    )
    _skew_verdict(metrics)


def _skew_verdict(metrics: Metrics) -> None:
    """The frequency-domain skew verdict (M13c), and what it is allowed to claim.

    Three outcomes, and the third is the honest one that a scalar could not
    express:

    * **pass** -- the fitted delay is inside the class's budget.
    * **warn** -- it is outside. A warn and never an error: promoting this needs
      the fit's false-positive behaviour measured across a full board first, which
      M13c put out of scope on purpose.
    * **under-floor** -- the skew term never rises far enough above the fitted
      floor for the delay to be a measurement. The fit still returns a number, and
      that number is an *upper bound*: whatever the skew is, it is small enough to
      be buried in what the rest of the board converts. Calling that a pass would
      be claiming a measurement nobody made.
    """
    fit = metrics.skew_fit
    fitted = metrics.fitted_skew_mm
    if fit is None or fitted is None or metrics.max_skew_mm is None:
        metrics.verdicts["skew_fit"] = "no-fit"
        return
    if not fit.confident:
        metrics.verdicts["skew_fit"] = "under-floor"
        metrics.notes.append(
            f"the fitted skew term peaks only {fit.peak_over_floor_db:.1f} dB above "
            f"the {fit.floor_db:.1f} dB mode-conversion floor, so the fitted "
            f"{fit.delay_ps:.2f} ps is an upper bound rather than a measurement"
        )
        return
    metrics.verdicts["skew_fit"] = (
        "pass" if fitted <= metrics.max_skew_mm else "warn"
    )


def write_touchstone(sp: SParameters, path: Path, reference_ohm: float) -> None:
    """The raw matrix as a Touchstone file, for scikit-rf and anything else.

    Only the excited columns were computed, so the rest are written as zero and the
    header says so. That is honest about what a two-excitation run knows: a full
    four-port sweep would need four runs and buys nothing for a differential pair,
    where the two undriven columns are the reciprocal of the two driven ones.
    """
    ports = sp.ports
    lines = [
        "! Generated by aipcb simulate (M12).",
        f"! {len(sp.frequencies)} points, {ports} ports, reference {reference_ohm} ohm.",
        "! Columns for excitations that were not run are written as zero:",
        f"! excited ports (0-based) were {', '.join(str(p) for p in sp.excited)}.",
        f"# HZ S RI R {reference_ohm:g}",
    ]
    for index, frequency in enumerate(sp.frequencies):
        cells = [f"{frequency:.6e}"]
        for out in range(ports):
            for into in range(ports):
                value = sp.values.get((out, into))
                sample = value[index] if value is not None and index < len(value) else 0j
                cells.append(f"{sample.real:.6e} {sample.imag:.6e}")
        lines.append(" ".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

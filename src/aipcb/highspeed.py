# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Controlled impedance: from a net class and a stackup to a trace geometry.

M11a's rule in one place, so that the router, the validator and the verification
report all derive the same numbers from the same inputs. Three of them read this
module and none of them computes a width of its own -- which is the point, because
an audit that recomputes the target with a different formula is auditing the
arithmetic rather than the board.

The derivation:

1. the class names a differential target with ``impedance_diff_ohm``;
2. the pair's *gap* comes from ``diff_pair_gap_mm`` if the source states one, and
   from the class clearance otherwise -- a gap is a manufacturing choice, and
   solving for both width and gap at once has no unique answer;
3. whether ground will be **poured alongside** the pair is read off the ``pours:``
   blocks, and how far away it will sit is the larger of the pour's clearance and
   the class's own -- KiCad enforces the larger of two nets' rules, and this has
   to predict what KiCad will do because the width is derived before any copper
   exists (M13b);
4. the *width* is then solved for, against the dielectric between the pair's layer
   and its reference plane: with :mod:`aipcb.impedance`'s coplanar model when
   there is a pour, and with the bare IPC-2141 microstrip when there is not;
5. an explicit ``diff_pair_width_mm`` overrides the solved width, and disagreeing
   with it by more than :data:`GEOMETRY_TOLERANCE` is reported.

Step 3 is M13b's whole subject. M12 simulated every pair on `examples/pcie-sata`
and every one came back below its declared target -- `REFCLKP/N` at 50.9 ohm
against 85 -- because the width had been derived for a bare microstrip and the
board pours ground 0.15 mm from it on both sides. Which model a class used is
therefore not an implementation detail; it is carried on the target, reported in
``check``'s output, and named in every diagnostic that quotes a number.

Nothing here decides anything: it returns a target, and the callers decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipcb.impedance import (
    DiffGeometry,
    ImpedanceUnreachable,
    cpwg_differential,
    differential_impedance,
    solve_width,
)
from aipcb.model.layout import Layout, NetClass, Stackup
from aipcb.netlist import Netlist

__all__ = [
    "GAP_TOLERANCE_MM",
    "GEOMETRY_TOLERANCE",
    "POUR_SENSITIVITY",
    "ImpedanceTarget",
    "controlled_classes",
    "pour_gap_for",
    "target_for",
]

#: How far an explicit width or gap may sit from the derived one before it is
#: reported. Ten percent is the milestone's number, and it is about right: etch
#: tolerance on a normal process is a few percent, so ten is "you meant something
#: else" rather than "the fabricator will vary".
GEOMETRY_TOLERANCE = 0.10

#: One standard etch tolerance on a pour-to-track gap, in millimetres. A normal
#: process holds a feature to about +/-0.025 mm (1 mil), and that is what M13b's
#: sensitivity warning perturbs the gap by. It is a *fabrication* number rather
#: than a design one, which is why it lives here as a constant with its source
#: named rather than as a field somebody has to set.
GAP_TOLERANCE_MM = 0.025

#: How much a controlled-impedance class's impedance may move under one etch
#: tolerance on the pour gap before validation says so, as a fraction. Half of
#: :data:`GEOMETRY_TOLERANCE`: a class whose gap alone can eat five percent of a
#: ten percent budget has spent it before the trace is drawn.
POUR_SENSITIVITY = 0.05


@dataclass(frozen=True, slots=True)
class ImpedanceTarget:
    """What a controlled-impedance class asks for, resolved against the stackup."""

    net_class: str
    target_ohm: float
    layer: str
    """The layer the derivation assumes the pair runs on."""
    reference: str | None
    """The plane the pair is referenced to. ``None`` means nothing declared one."""
    geometry: DiffGeometry
    """The derived width and gap, and what they actually come out at."""
    width_override_mm: float | None = None
    gap_override_mm: float | None = None
    unreachable: str | None = None
    """Set when the target cannot be met at this gap on this stackup."""

    @property
    def model(self) -> str:
        """``cpwg`` when ground will be poured alongside, ``microstrip`` when not."""
        return self.geometry.model

    @property
    def pour_gap_mm(self) -> float | None:
        """How far the pour will sit from each side, or ``None`` if there is none."""
        return self.geometry.pour_gap_mm

    @property
    def width_mm(self) -> float:
        """The width the board will actually be built with."""
        return self.width_override_mm or self.geometry.width_mm

    @property
    def gap_mm(self) -> float:
        return self.gap_override_mm or self.geometry.gap_mm

    @property
    def width_deviation(self) -> float:
        """Fractional distance from the derived width, signed."""
        derived = self.geometry.width_mm
        if derived <= 0:
            return 0.0
        return (self.width_mm - derived) / derived

    def _impedance(self, pour_gap: float | None) -> float:
        if pour_gap is None:
            return differential_impedance(
                self.width_mm,
                self.gap_mm,
                self.geometry.height_mm,
                self.geometry.copper_mm,
                self.geometry.epsilon_r,
            )
        return cpwg_differential(
            self.width_mm,
            self.gap_mm,
            self.geometry.height_mm,
            self.geometry.copper_mm,
            self.geometry.epsilon_r,
            pour_gap,
        )

    @property
    def actual_ohm(self) -> float:
        """What the geometry the board will be built with actually comes out at."""
        return round(self._impedance(self.pour_gap_mm), 2)

    @property
    def gap_sensitivity(self) -> float | None:
        """How far one etch tolerance on the *pour* gap moves the impedance.

        Returned as a fraction of the class's target, worst of the two directions.
        ``None`` for a class with no pour beside it, where the question does not
        arise. This is the coupling M13b introduced and it is worth surfacing: the
        pour clearance used to be a DRC number and is now an impedance input, so a
        class that holds its target only at exactly the nominal gap should say so
        before the board is made rather than after.
        """
        gap = self.pour_gap_mm
        if gap is None or not self.target_ohm:
            return None
        tighter = self._impedance(max(gap - GAP_TOLERANCE_MM, 1e-3))
        looser = self._impedance(gap + GAP_TOLERANCE_MM)
        nominal = self._impedance(gap)
        return max(abs(tighter - nominal), abs(looser - nominal)) / self.target_ohm

    def to_dict(self) -> dict[str, object]:
        return {
            "net_class": self.net_class,
            "target_ohm": self.target_ohm,
            "layer": self.layer,
            "reference": self.reference,
            "model": self.model,
            "pour_gap_mm": self.pour_gap_mm,
            "gap_sensitivity": (
                None
                if self.gap_sensitivity is None
                else round(self.gap_sensitivity, 4)
            ),
            "derived_width_mm": self.geometry.width_mm,
            "gap_mm": self.gap_mm,
            "width_mm": self.width_mm,
            "height_mm": self.geometry.height_mm,
            "epsilon_r": self.geometry.epsilon_r,
            "actual_ohm": self.actual_ohm,
            "unreachable": self.unreachable,
        }


def _layout_of(netlist: Netlist) -> Layout | None:
    return netlist.layout


def _assumed_layer(net_class: NetClass, stackup: Stackup) -> str:
    """The layer the derivation assumes.

    A controlled-impedance class normally names one, because a pair whose
    reference changes under it is not controlled at all. Without a preference the
    front is assumed and the target says so, so a reader can see the assumption
    rather than inherit it.
    """
    signal = stackup.signal_layers
    for layer in net_class.prefer_layers:
        if layer in signal:
            return layer
    return signal[0] if signal else "F.Cu"


def pour_gap_for(netlist: Netlist, layer: str, clearance_mm: float) -> float | None:
    """How far poured copper of *another* net will sit from a trace on ``layer``.

    ``None`` when nothing is poured there, which is the answer that keeps a design
    on the bare-microstrip model.

    Two rules, and both are about predicting what KiCad will actually do, because
    the width has to be derived before any copper exists:

    * **The larger of the two clearances wins.** KiCad enforces the greater of the
      zone's own figure and the clearance constraint between the two nets, so this
      takes the maximum of the pour's clearance (its own, or its net class's) and
      the controlled-impedance class's.
    * **The nearest pour is the one that matters.** A layer with two pours on it
      is a layer where whichever comes closest sets the coplanar gap.

    What this deliberately does *not* do is reason about where a pour's region
    reaches. A ``scope: region`` pour on the pair's layer is counted whether or
    not the pair runs through that region, because the pair's route does not exist
    yet. The consequence is stated rather than hidden: on a board where the pour
    is a patch somewhere else, the derivation is conservative in the direction of
    a narrower trace.
    """
    gaps: list[float] = []
    for pour in netlist.pours:
        if layer not in pour.copper_layers:
            continue
        elaborated = netlist.nets.get(pour.net)
        rules = (
            netlist.net_classes.get(elaborated.net_class, NetClass())
            if elaborated is not None
            else NetClass()
        )
        gaps.append(max(pour.clearance or rules.clearance_mm, clearance_mm))
    return min(gaps) if gaps else None


def target_for(
    netlist: Netlist, class_name: str, net_class: NetClass | None = None
) -> ImpedanceTarget | None:
    """Resolve one class's controlled-impedance target, or ``None`` if it has none."""
    rules = net_class if net_class is not None else netlist.net_classes.get(class_name)
    if rules is None or rules.impedance_diff_ohm is None:
        return None

    layout = _layout_of(netlist)
    stackup = layout.stackup if layout is not None else Stackup()
    layer = _assumed_layer(rules, stackup)
    reference = rules.reference or stackup.reference_below(layer)

    if reference is not None:
        dielectric = stackup.dielectric_between(layer, reference)
    else:
        # Nothing declared a plane. The far side of the board is the only thing
        # a return current can use, so that is what the geometry is derived
        # against -- and the report says the reference is undeclared.
        far = stackup.signal_layers[-1] if stackup.signal_layers else "B.Cu"
        dielectric = stackup.dielectric_between(layer, far)

    gap = rules.diff_pair_gap_mm or rules.clearance_mm
    copper = stackup.copper_thickness_mm(layer)
    pour_gap = pour_gap_for(netlist, layer, rules.clearance_mm)
    try:
        geometry = solve_width(
            rules.impedance_diff_ohm,
            gap,
            dielectric.thickness_mm,
            copper,
            dielectric.epsilon_r,
            pour_gap,
        )
        unreachable = None
    except ImpedanceUnreachable as exc:
        geometry = DiffGeometry(
            width_mm=rules.diff_pair_width_mm or rules.trace_width_mm,
            gap_mm=round(gap, 4),
            height_mm=dielectric.thickness_mm,
            epsilon_r=dielectric.epsilon_r,
            copper_mm=copper,
            impedance_ohm=0.0,
            pour_gap_mm=None if pour_gap is None else round(pour_gap, 4),
        )
        unreachable = str(exc)

    return ImpedanceTarget(
        net_class=class_name,
        target_ohm=rules.impedance_diff_ohm,
        layer=layer,
        reference=reference,
        geometry=geometry,
        width_override_mm=rules.diff_pair_width_mm,
        gap_override_mm=rules.diff_pair_gap_mm,
        unreachable=unreachable,
    )


def controlled_classes(netlist: Netlist) -> dict[str, ImpedanceTarget]:
    """Every controlled-impedance class in the design, resolved. Sorted by name."""
    out: dict[str, ImpedanceTarget] = {}
    for name in sorted(netlist.net_classes):
        target = target_for(netlist, name)
        if target is not None:
            out[name] = target
    return out

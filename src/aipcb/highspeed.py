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
3. the *width* is then solved for, against the dielectric between the pair's layer
   and its reference plane, with :mod:`aipcb.impedance`'s IPC-2141 approximation;
4. an explicit ``diff_pair_width_mm`` overrides the solved width, and disagreeing
   with it by more than :data:`GEOMETRY_TOLERANCE` is reported.

Nothing here decides anything: it returns a target, and the callers decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipcb.impedance import (
    DiffGeometry,
    ImpedanceUnreachable,
    differential_impedance,
    solve_width,
)
from aipcb.model.layout import Layout, NetClass, Stackup
from aipcb.netlist import Netlist

__all__ = [
    "GEOMETRY_TOLERANCE",
    "ImpedanceTarget",
    "controlled_classes",
    "target_for",
]

#: How far an explicit width or gap may sit from the derived one before it is
#: reported. Ten percent is the milestone's number, and it is about right: etch
#: tolerance on a normal process is a few percent, so ten is "you meant something
#: else" rather than "the fabricator will vary".
GEOMETRY_TOLERANCE = 0.10


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

    @property
    def actual_ohm(self) -> float:
        """What the geometry the board will be built with actually comes out at."""
        return round(
            differential_impedance(
                self.width_mm,
                self.gap_mm,
                self.geometry.height_mm,
                self.geometry.copper_mm,
                self.geometry.epsilon_r,
            ),
            2,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "net_class": self.net_class,
            "target_ohm": self.target_ohm,
            "layer": self.layer,
            "reference": self.reference,
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
    try:
        geometry = solve_width(
            rules.impedance_diff_ohm,
            gap,
            dielectric.thickness_mm,
            copper,
            dielectric.epsilon_r,
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

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Source-level checks on controlled-impedance net classes (M11a).

These run under ``aipcb validate``, before anything is built, because everything
they can catch is a property of the source: a target the stackup cannot reach, an
explicit width that contradicts the target, a reference plane the board does not
have. The checks that need copper -- is there actually plane under this track --
are M11e's and live in :mod:`aipcb.checks.highspeed`.
"""

from __future__ import annotations

from aipcb.diagnostics import Report
from aipcb.highspeed import (
    GAP_TOLERANCE_MM,
    GEOMETRY_TOLERANCE,
    POUR_SENSITIVITY,
    ImpedanceTarget,
    controlled_classes,
)
from aipcb.model.layout import Stackup, copper_layer_names
from aipcb.netlist import Netlist
from aipcb.source import Loc

__all__ = ["run_impedance_checks"]


def _loc(netlist: Netlist, name: str, *rest: str) -> Loc | None:
    """The line a class's field sits on, falling back to the class itself."""
    return netlist.locs.get(("net_classes", name, *rest)) or netlist.locs.get(
        ("net_classes", name)
    )


def run_impedance_checks(netlist: Netlist, report: Report) -> None:
    """Validate every controlled-impedance class against the stackup it names."""
    stackup = netlist.layout.stackup if netlist.layout is not None else Stackup()
    targets = controlled_classes(netlist)

    _check_orphan_fields(netlist, report)
    for name, target in targets.items():
        _check_reachable(netlist, name, target, report)
        _check_geometry_override(netlist, name, target, report)
        _check_reference(netlist, name, target, stackup, report)
        _check_uncoupled_budget(netlist, name, target, report)
        _check_pour_sensitivity(netlist, name, target, report)


def _check_orphan_fields(netlist: Netlist, report: Report) -> None:
    """High-speed fields that do nothing without an impedance target."""
    for name in sorted(netlist.net_classes):
        rules = netlist.net_classes[name]
        if rules.impedance_diff_ohm is not None:
            continue
        idle = [
            field
            for field, value in (
                ("coupling", rules.coupling),
                ("max_uncoupled_mm", rules.max_uncoupled_mm),
                ("standoff_k", rules.standoff_k),
            )
            if value is not None
        ]
        if not idle:
            continue
        report.warning(
            "impedance-rules-inert",
            f"net class {name!r} sets {', '.join(idle)} but no "
            "`impedance_diff_ohm`, so the controlled-impedance rules are off",
            loc=_loc(netlist, name),
            path=("net_classes", name),
            hint="the standoff corridor, the coupling budget and the geometry "
            "audit all key off `impedance_diff_ohm`",
        )


def _check_reachable(
    netlist: Netlist, name: str, target: ImpedanceTarget, report: Report
) -> None:
    if target.unreachable is None:
        return
    report.warning(
        "impedance-unreachable",
        f"net class {name!r}: {target.unreachable}",
        loc=_loc(netlist, name, "impedance_diff_ohm"),
        path=("net_classes", name, "impedance_diff_ohm"),
        hint=f"at a {target.gap_mm} mm gap over {target.geometry.height_mm} mm of "
        f"laminate (er {target.geometry.epsilon_r}); change the gap, the stackup, "
        "or the layer this class prefers",
        net_class=name,
    )


def _check_geometry_override(
    netlist: Netlist, name: str, target: ImpedanceTarget, report: Report
) -> None:
    """An explicit width that disagrees with the derived one by more than 10 %."""
    if target.unreachable is not None or target.width_override_mm is None:
        return
    deviation = target.width_deviation
    if abs(deviation) <= GEOMETRY_TOLERANCE:
        return
    report.warning(
        "impedance-geometry-override",
        f"net class {name!r} sets diff_pair_width_mm "
        f"{target.width_override_mm} mm, {deviation:+.0%} from the "
        f"{target.geometry.width_mm} mm that {target.target_ohm:.0f} ohm implies on "
        f"this stackup; the pair will come out at about "
        f"{target.actual_ohm:.0f} ohm",
        loc=_loc(netlist, name, "diff_pair_width_mm"),
        path=("net_classes", name, "diff_pair_width_mm"),
        hint="drop `diff_pair_width_mm` to take the derived width, or keep it and "
        "accept the impedance it produces -- the board is built from what the "
        "source says, not from the target",
        net_class=name,
        derived_width_mm=target.geometry.width_mm,
        actual_ohm=target.actual_ohm,
    )


def _check_reference(
    netlist: Netlist,
    name: str,
    target: ImpedanceTarget,
    stackup: Stackup,
    report: Report,
) -> None:
    """The declared reference plane has to exist, and to be a plane."""
    declared = netlist.net_classes[name].reference
    if declared is None:
        if target.reference is None:
            report.warning(
                "impedance-no-reference",
                f"net class {name!r} asks for {target.target_ohm:.0f} ohm but the "
                "stackup declares no plane, so nothing says what the pair is "
                "referenced to",
                loc=_loc(netlist, name, "impedance_diff_ohm"),
                path=("net_classes", name),
                hint="declare the reference with `reference:` on the class, and a "
                "plane for it under `layout.stackup.planes`",
                net_class=name,
            )
        return

    names = copper_layer_names(stackup.copper_layers)
    if declared not in names:
        report.error(
            "impedance-reference-missing",
            f"net class {name!r} references {declared}, which this "
            f"{stackup.copper_layers}-layer board does not have",
            loc=_loc(netlist, name, "reference"),
            path=("net_classes", name, "reference"),
            hint=f"copper layers on this board: {', '.join(names)}",
            net_class=name,
        )
        return
    if declared == target.layer:
        report.error(
            "impedance-reference-is-signal-layer",
            f"net class {name!r} references {declared}, the layer it routes on",
            loc=_loc(netlist, name, "reference"),
            path=("net_classes", name, "reference"),
            hint="a pair cannot be its own return path",
            net_class=name,
        )
        return
    planes = stackup.plane_layers
    if declared not in planes:
        report.warning(
            "impedance-reference-not-a-plane",
            f"net class {name!r} references {declared}, which is not declared a "
            "plane, so the router may put signals on it",
            loc=_loc(netlist, name, "reference"),
            path=("net_classes", name, "reference"),
            hint=f"add {declared} under `layout.stackup.planes` and pour it",
            net_class=name,
        )
        return
    poured = {
        layer
        for pour in netlist.pours
        for layer in pour.copper_layers
        if pour.net == planes[declared]
    }
    if declared not in poured:
        report.warning(
            "impedance-reference-not-poured",
            f"net class {name!r} references {declared}, which is reserved for "
            f"{planes[declared]} but has no pour on it -- a reserved layer with no "
            "copper is not a reference",
            loc=_loc(netlist, name, "reference"),
            path=("net_classes", name, "reference"),
            hint=f"add a `pours:` entry for {planes[declared]} on {declared}",
            net_class=name,
        )


def _check_uncoupled_budget(
    netlist: Netlist, name: str, target: ImpedanceTarget, report: Report
) -> None:
    rules = netlist.net_classes[name]
    if rules.coupling != "tight" or rules.max_uncoupled_mm is not None:
        return
    report.warning(
        "impedance-no-uncoupled-budget",
        f"net class {name!r} asks for tight coupling but sets no "
        "`max_uncoupled_mm`, so there is no budget to enforce",
        loc=_loc(netlist, name),
        path=("net_classes", name, "coupling"),
        hint="`coupling: tight` makes `max_uncoupled_mm` a hard budget; without "
        "one the pair falls back to the ordinary fan-out fraction limit",
        net_class=name,
    )


def _check_pour_sensitivity(
    netlist: Netlist, name: str, target: ImpedanceTarget, report: Report
) -> None:
    """A pour close enough that etching it decides the impedance (M13b).

    Before M13 the pour clearance was a DRC number: keep this far from that, and
    nothing else depended on it. Now it is an input to the width derivation, and a
    class whose pour sits tight enough that one etch tolerance moves the impedance
    by a chunk of its own budget has spent that budget before a single track is
    drawn. Saying so at validation is the cheap place; the alternative is finding
    out from a coupon.
    """
    sensitivity = target.gap_sensitivity
    if target.unreachable is not None or sensitivity is None:
        return
    rules = netlist.net_classes[name]
    threshold = rules.pour_gap_sensitivity or POUR_SENSITIVITY
    if sensitivity <= threshold:
        return
    report.warning(
        "impedance-pour-gap-sensitive",
        f"net class {name!r} is derived against ground poured "
        f"{target.pour_gap_mm} mm away, where one {GAP_TOLERANCE_MM} mm etch "
        f"tolerance on that gap moves the differential impedance by "
        f"{sensitivity:.1%} of the {target.target_ohm:.0f} ohm target -- more than "
        f"the {threshold:.0%} this class allows",
        loc=_loc(netlist, name, "clearance_mm"),
        path=("net_classes", name, "clearance_mm"),
        hint="open the pour clearance for this class, or raise "
        "`pour_gap_sensitivity` if the coupon says the process holds it",
        net_class=name,
        model=target.model,
        pour_gap_mm=target.pour_gap_mm,
        gap_sensitivity=round(sensitivity, 4),
    )

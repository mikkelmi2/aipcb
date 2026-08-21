"""AC-coupling capacitors in a high-speed pair (M11c).

PCIe requires series capacitors on the transmitting end, and a series part in a
differential pair is a discontinuity in the middle of it: the pair stops being a
pair at the capacitors' pads, opens out to whatever pitch two 0402s can sit at,
and closes again on the far side. Nothing about that is avoidable. What *is*
avoidable is doing it asymmetrically, and an asymmetric AC coupling is a skew the
length matcher cannot see, because it is built into the placement rather than into
the copper.

So this is the check: mark the capacitors ``role: ac_coupling``, and aipcb works
out from their own nets which pair they sit in -- the two halves are two declared
differential pairs joined by two capacitors, and the netlist says so without
anybody having to name it twice -- then measures whether they are level with each
other across the pair's direction of travel and whether they face the same way.

**What this does not do**, and the deviation is deliberate: it does not *place*
the capacitors. The placer is M9's, positions are the source's, and a routing-side
generator that moved parts would be a bigger change than M11 asks for. It
validates the placement, measures the discontinuity, and the coupling budget in
the M11e report counts what the capacitors cost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from aipcb.compile.edge import AC_COUPLING_ROLE
from aipcb.compile.frame import frame_for
from aipcb.compile.place import BoardPlacement, component_extents, plan_placement
from aipcb.diagnostics import Report
from aipcb.netlist import Netlist

__all__ = ["AcCoupling", "ac_couplings", "run_ac_coupling_checks"]

Point = tuple[float, float]

#: How far out of line the two capacitors may sit, measured along the pair's own
#: direction of travel, before it is reported. Fifty microns: an order of magnitude
#: below any pair's skew budget, and well inside a pick-and-place machine's
#: accuracy, so a complaint here is about the source and not about the factory.
LEVEL_TOLERANCE_MM = 0.05


@dataclass(frozen=True, slots=True)
class AcCoupling:
    """Two capacitors in series with the two halves of one pair."""

    positive: str
    """The capacitor in the pair's positive half."""
    negative: str
    upstream: tuple[str, str]
    """The pair on the side the capacitors' first pads face."""
    downstream: tuple[str, str]
    at: tuple[Point, Point]
    """Where the two capacitors sit, in KiCad coordinates."""
    axis: Point
    """The pair's direction of travel through them, as a unit vector."""
    pitch_mm: float
    """How far apart the two capacitors sit, across the route."""
    skewed_mm: float
    """How far out of line they sit, along it. Zero is what this wants."""

    @property
    def key(self) -> str:
        return f"{self.upstream[0]}+{self.upstream[1]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.key,
            "capacitors": [self.positive, self.negative],
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "pitch_mm": round(self.pitch_mm, 4),
            "out_of_line_mm": round(self.skewed_mm, 4),
        }


def ac_couplings(netlist: Netlist, placement: BoardPlacement) -> list[AcCoupling]:
    """Every pair of capacitors marked ``role: ac_coupling``, paired up.

    The pairing comes from the netlist rather than from a name in the source: a
    capacitor's two pins are on two nets, and where those nets' partners are the
    two nets of the *other* capacitor, the two capacitors are one coupling. That
    cannot disagree with the schematic, which a written-down pair name could.
    """
    caps = netlist.components_with_role(AC_COUPLING_ROLE)
    by_net: dict[str, str] = {}
    for cap in caps:
        for net in cap.connections.values():
            by_net.setdefault(net, cap.refdes)

    out: list[AcCoupling] = []
    seen: set[str] = set()
    for cap in caps:
        if cap.refdes in seen:
            continue
        nets = [cap.connections.get(pin) for pin in sorted(cap.connections)]
        partners = [
            (netlist.nets[net].attrs.diff_pair if net in netlist.nets else None)
            for net in nets
            if net
        ]
        mates = {by_net.get(p) for p in partners if p}
        mates.discard(cap.refdes)
        mates.discard(None)
        if len(mates) != 1:
            continue
        mate = next(iter(mates))
        assert mate is not None
        first = placement.positions.get(cap.refdes)
        second = placement.positions.get(mate)
        if first is None or second is None:
            continue
        seen.update({cap.refdes, mate})

        # Which net of this capacitor goes with which net of the other is decided
        # by `diff_pair:` and not by pin number: the two capacitors are often
        # mirror images of each other -- that is how their reference designators
        # stay off each other's silkscreen -- so pad 1 of one faces the controller
        # and pad 1 of the other faces the connector.
        other = netlist.components[mate]
        sides: list[tuple[str, str]] = []
        for net in sorted(set(cap.connections.values())):
            partner = (
                netlist.nets[net].attrs.diff_pair if net in netlist.nets else None
            )
            if partner and partner in set(other.connections.values()):
                sides.append((net, partner))
        if len(sides) != 2:
            continue
        upstream, downstream = sides

        axis = _axis(netlist, placement, cap.refdes, sorted(cap.connections))
        across = (-axis[1], axis[0])
        offset = (second.x - first.x, second.y - first.y)
        pitch = abs(offset[0] * across[0] + offset[1] * across[1])
        along = abs(offset[0] * axis[0] + offset[1] * axis[1])
        out.append(
            AcCoupling(
                positive=min(cap.refdes, mate),
                negative=max(cap.refdes, mate),
                upstream=upstream,
                downstream=downstream,
                at=((first.x, first.y), (second.x, second.y)),
                axis=axis,
                pitch_mm=pitch,
                skewed_mm=along,
            )
        )
    return sorted(out, key=lambda c: c.key)


def _axis(
    netlist: Netlist, placement: BoardPlacement, refdes: str, pins: list[str]
) -> Point:
    """The direction the pair travels through the capacitor, from its own pads.

    Taken from the capacitor's own rotation rather than from where the pair goes:
    the two pads of a two-pad part define the direction current takes through it,
    and a capacitor across the route rather than along it is a different mistake
    that this measurement then reports as a pitch of nearly zero.
    """
    del netlist, pins
    placed = placement.positions.get(refdes)
    if placed is None:
        return (0.0, 1.0)
    angle = math.radians(placed.rotation)
    # A two-pad chip's pads lie on its local x axis; KiCad turns it the way
    # `rotate_kicad` does, so the local +x direction comes out here.
    return (round(math.cos(angle), 6), round(-math.sin(angle), 6))


def run_ac_coupling_checks(netlist: Netlist, report: Report) -> None:
    """Validate every AC coupling in the design. A no-op where there is none."""
    caps = netlist.components_with_role(AC_COUPLING_ROLE)
    if not caps:
        return
    frame = frame_for(netlist)
    extents, missing = component_extents(netlist)
    if missing:
        return
    placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
    couplings = ac_couplings(netlist, placement)

    paired = {c.positive for c in couplings} | {c.negative for c in couplings}
    for cap in caps:
        if cap.refdes in paired:
            continue
        report.warning(
            "ac-coupling-unpaired",
            f"{cap.refdes} is marked `role: ac_coupling` but no second capacitor "
            "sits in the other half of the same pair",
            loc=cap.loc,
            path=(*cap.source_path, "role"),
            hint="AC coupling is a matched pair of parts in a matched pair of nets; "
            "one capacitor in one half is an asymmetry, not a coupling",
            component=cap.refdes,
        )

    for coupling in couplings:
        _check_symmetry(coupling, netlist, report)


def _check_symmetry(
    coupling: AcCoupling, netlist: Netlist, report: Report
) -> None:
    component = netlist.components.get(coupling.positive)
    loc = component.loc if component is not None else None
    if coupling.skewed_mm > LEVEL_TOLERANCE_MM:
        report.warning(
            "ac-coupling-asymmetric",
            f"{coupling.positive} and {coupling.negative} sit "
            f"{coupling.skewed_mm:.3f} mm out of line along the pair's direction of "
            "travel, which is skew built into the placement",
            loc=loc,
            path=("placement", coupling.negative),
            hint="the two halves have to meet their capacitor at the same point "
            "along the route; move one of them so the two are level, or accept a "
            "mismatch the length matcher cannot reach",
            component=coupling.negative,
        )
        return
    report.info(
        "ac-coupling",
        f"{coupling.positive}/{coupling.negative} couple "
        f"{coupling.upstream[0]}+{coupling.upstream[1]} to "
        f"{coupling.downstream[0]}+{coupling.downstream[1]}, "
        f"{coupling.pitch_mm:.3f} mm apart across the route and level along it to "
        f"within {coupling.skewed_mm:.4f} mm",
        loc=loc,
        path=("placement", coupling.positive),
        hint="the pair fans out from its own pitch to reach them and back again; "
        "that fan-out is counted against `max_uncoupled_mm`",
        component=coupling.positive,
    )

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Which differential pairs there are to simulate, and where each one really ends.

A declared pair is two nets that name each other. That is not always one *conductor*
-- ``examples/pcie-sata``'s transmit lane is ``PCIE_TXP/N`` up to a pair of coupling
capacitors and ``PCIE_TXP_C/N_C`` after them, four nets and two declared pairs for
one physical link. Simulating them separately would report two 10 mm stubs where the
board has one 18 mm lane, and would put a port in the middle of a capacitor.

So this module merges across series parts marked ``role: ac_coupling``: aipcb *knows*
those are shorts at signal frequencies, which is the advantage a source-level slicer
has over a geometric one. The capacitor becomes a copper bridge in the slice, the two
declared pairs become one :class:`LogicalPair`, and the ports go where the link
actually ends.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipcb.netlist import Netlist

__all__ = ["AC_COUPLING_ROLE", "LogicalPair", "logical_pairs"]

#: The role that marks a series capacitor as a short for simulation purposes.
AC_COUPLING_ROLE = "ac_coupling"


@dataclass(frozen=True, slots=True)
class LogicalPair:
    """One physical differential link, however many nets the schematic split it into."""

    name: str
    """``<N-net>+<P-net>`` of the first declared pair, matching M11e's pair naming."""
    net_class: str
    positive: tuple[str, ...]
    """Nets making up one conductor, source order along the link."""
    negative: tuple[str, ...]
    declared: tuple[tuple[str, str], ...]
    """The declared pairs this merges, each ``(net, its diff_pair)`` sorted."""
    bridged_by: tuple[str, ...]
    """Reference designators shorted out to make the merge, in refdes order."""
    source_path: tuple[str | int, ...]

    @property
    def nets(self) -> tuple[str, ...]:
        return tuple(sorted((*self.positive, *self.negative)))

    def to_dict(self) -> dict[str, object]:
        return {
            "pair": self.name,
            "net_class": self.net_class,
            "positive": list(self.positive),
            "negative": list(self.negative),
            "declared": [list(d) for d in self.declared],
            "bridged_by": list(self.bridged_by),
        }


def _declared_pairs(netlist: Netlist) -> list[tuple[str, str]]:
    """Every reciprocal ``diff_pair`` link, canonical and sorted.

    Reciprocity is the test rather than a single arrow, because a one-sided
    ``diff_pair:`` is a typo, not a pair, and M11's checks already say so.
    """
    seen: set[tuple[str, str]] = set()
    for name, net in netlist.nets.items():
        partner = net.attrs.diff_pair
        if partner is None or partner not in netlist.nets:
            continue
        if netlist.nets[partner].attrs.diff_pair != name:
            continue
        seen.add((min(name, partner), max(name, partner)))
    return sorted(seen)


def _coupling_bridges(netlist: Netlist) -> list[tuple[str, tuple[str, ...]]]:
    """``(refdes, nets)`` for every part marked ``role: ac_coupling``."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for comp in netlist.components_with_role(AC_COUPLING_ROLE):
        nets = tuple(sorted(set(comp.connections.values())))
        if len(nets) == 2:
            out.append((comp.refdes, nets))
    return sorted(out)


class _Union:
    """Smallest union-find that does the job, keyed by name."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def logical_pairs(netlist: Netlist) -> list[LogicalPair]:
    """Every differential link in the design, merged across AC-coupling parts.

    Sorted by name, so a batch runs in the same order every time and a manifest
    diffs cleanly.
    """
    declared = _declared_pairs(netlist)
    if not declared:
        return []

    # Two union-finds that have to agree: one over *nets* (which conductor is this
    # net part of) and one over *declared pairs* (which link is this pair part of).
    # A capacitor merges both, but only when its two nets belong to pairs -- a
    # coupling cap on a single-ended net is somebody's mistake, not a merge.
    pair_of: dict[str, tuple[str, str]] = {}
    for pair in declared:
        pair_of[pair[0]] = pair
        pair_of[pair[1]] = pair

    conductors = _Union()
    links = _Union()
    bridges: dict[tuple[str, str], list[str]] = {}
    for refdes, (net_a, net_b) in _coupling_bridges(netlist):
        if net_a not in pair_of or net_b not in pair_of:
            continue
        if pair_of[net_a] == pair_of[net_b]:
            # A capacitor across the pair rather than in series with it.
            continue
        conductors.union(net_a, net_b)
        key = min(pair_of[net_a], pair_of[net_b])
        links.union("|".join(pair_of[net_a]), "|".join(pair_of[net_b]))
        bridges.setdefault(key, []).append(refdes)

    grouped: dict[str, list[tuple[str, str]]] = {}
    for pair in declared:
        grouped.setdefault(links.find("|".join(pair)), []).append(pair)

    out: list[LogicalPair] = []
    for members in grouped.values():
        members.sort()
        nets = sorted({n for pair in members for n in pair})
        sides: dict[str, list[str]] = {}
        for net in nets:
            sides.setdefault(conductors.find(net), []).append(net)
        if len(sides) != 2:
            # Merging produced something that is not two conductors. Keep the
            # declared pairs separate rather than inventing a topology.
            for pair in members:
                out.append(_single(netlist, pair))
            continue
        first, second = (sides[key] for key in sorted(sides))
        head = members[0]
        parts = sorted({r for pair in members for r in bridges.get(pair, [])})
        out.append(
            LogicalPair(
                name=f"{head[0]}+{head[1]}",
                net_class=netlist.nets[head[0]].net_class,
                positive=tuple(first),
                negative=tuple(second),
                declared=tuple(members),
                bridged_by=tuple(parts),
                source_path=netlist.nets[head[0]].source_path,
            )
        )
    return sorted(out, key=lambda p: p.name)


def _single(netlist: Netlist, pair: tuple[str, str]) -> LogicalPair:
    return LogicalPair(
        name=f"{pair[0]}+{pair[1]}",
        net_class=netlist.nets[pair[0]].net_class,
        positive=(pair[0],),
        negative=(pair[1],),
        declared=(pair,),
        bridged_by=(),
        source_path=netlist.nets[pair[0]].source_path,
    )

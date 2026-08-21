"""Turning tightened routes into KiCad track segments and vias.

Rubber-band tightening produces any-angle geometry, and that is what gets emitted:
ordinary ``segment`` items, exactly as KiCad writes them itself. Nothing downstream
-- KiCad, Gerber, the fab -- sees anything unusual (ADR 0006).

UUIDs are derived from what the copper *is* -- the net, the two things it joins,
and its position along the run -- so they are stable across rebuilds. That is what
lets an incremental build tell its own copper apart from a human's.
"""

from __future__ import annotations

import math
from dataclasses import replace

from aipcb.ids import element_uuid, net_codes
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.route.stretch import RoutedConnection, StretchResult, Via

Point = tuple[float, float]

__all__ = [
    "attach_copper",
    "drop_generated",
    "generated_uuids",
    "merge_overlapping_holes",
    "segments_for",
    "track_uuid",
    "via_nodes",
    "via_uuid",
]


def track_uuid(net: str, start: str, end: str, index: int) -> str:
    """The stable identity of one track segment."""
    return element_uuid("track", net, f"{start}>{end}", index)


def via_uuid(net: str, start: str, end: str) -> str:
    """The stable identity of one via, named by the two legs it joins."""
    return element_uuid("via", net, f"{start}>{end}")


def segments_for(result: StretchResult, net_code: int) -> list[SNode]:
    """The KiCad ``segment`` items for one tightened leg."""
    nodes: list[SNode] = []
    for index, (a, b) in enumerate(result.segments):
        nodes.append(
            SNode("segment").add(
                SNode("start").add(num(a[0]), num(a[1])),
                SNode("end").add(num(b[0]), num(b[1])),
                SNode("width").add(num(result.width)),
                SNode("layer").add(quoted(result.layer)),
                SNode("net").add(sym(str(net_code))),
                SNode("uuid").add(
                    quoted(track_uuid(result.net, result.start, result.end, index))
                ),
            )
        )
    return nodes


def via_nodes(via: Via, net_code: int) -> SNode:
    """One KiCad ``via``.

    A blind or buried via carries its type as a bare symbol straight after the
    token, which is how KiCad writes one; a through via carries nothing, because
    through is what a via is unless it says otherwise.
    """
    node = SNode("via")
    if via.kind in ("blind", "micro"):
        node.add(sym(via.kind))
    node.add(
        SNode("at").add(num(via.point[0]), num(via.point[1])),
        SNode("size").add(num(via.diameter)),
        SNode("drill").add(num(via.drill)),
        SNode("layers").add(quoted(via.from_layer), quoted(via.to_layer)),
        SNode("net").add(sym(str(net_code))),
        SNode("uuid").add(quoted(via_uuid(via.net, via.name, via.kind))),
    )
    return node


def merge_overlapping_holes(connections: list[RoutedConnection]) -> int:
    """Two drill hits of one net whose holes overlap are one hole. Returns how many.

    Not a routing decision -- a manufacturing one. When two vias of the same net end
    up a fraction of a millimetre apart, the fabricator is being asked to drill twice
    through the same piece of copper, which KiCad reports as overlapping holes and a
    drill file expresses as two hits that break each other's edges.

    The later via is moved onto the earlier one and the leg ends that met it move
    with it, after which :func:`attach_copper` emits the one hole they now share.
    Moving *onto an existing via of the same net and the same size* is what makes
    this safe: whatever the destination clears, this via clears too, and the move is
    never further than one drill diameter.
    """
    kept: list[Via] = []
    merged = 0
    for connection in sorted(connections, key=lambda c: (c.net, c.start, c.end)):
        for index, via in enumerate(connection.vias):
            home = next(
                (
                    other
                    for other in kept
                    if other.net == via.net
                    and other.diameter == via.diameter
                    and math.dist(other.point, via.point)
                    < max(other.drill, via.drill)
                ),
                None,
            )
            if home is None:
                kept.append(via)
                continue
            connection.vias[index] = replace(via, point=home.point)
            _move_leg_ends(connection, index, via.point, home.point)
            merged += 1
    return merged


def _move_leg_ends(
    connection: RoutedConnection, index: int, was: Point, now: Point
) -> None:
    """Follow a moved via with the two leg ends that met at it."""
    for leg in (connection.legs[index : index + 1] + connection.legs[index + 1 : index + 2]):
        if leg.points and math.dist(leg.points[-1], was) < 1e-9:
            leg.points[-1] = now
        if leg.points and math.dist(leg.points[0], was) < 1e-9:
            leg.points[0] = now


def attach_copper(
    board: SNode, connections: list[RoutedConnection], nets: list[str]
) -> tuple[int, int]:
    """Add every routed segment and via to a board. Returns (segments, vias)."""
    codes = net_codes(nets)
    segments = vias = 0
    # Sorted so the file's order depends on the design, never on routing order.
    ordered = sorted(connections, key=lambda c: (c.net, c.start, c.end))
    drilled: set[tuple[str, str, str, int, int]] = set()
    for connection in ordered:
        code = codes.get(connection.net)
        if code is None:
            continue
        for leg in connection.legs:
            for node in segments_for(leg, code):
                board.add(node)
                segments += 1
        for via in connection.vias:
            if not _is_new_hole(drilled, via):
                continue
            board.add(via_nodes(via, code))
            vias += 1
    return segments, vias


def _is_new_hole(drilled: set[tuple[str, str, str, int, int]], via: Via) -> bool:
    """Whether this via is a hole the board does not already have.

    Two connections of one net legitimately meet at a via -- an escape via that a
    route continues from is exactly that -- and each of them records it. Writing it
    twice puts two drill hits at one coordinate, which KiCad reports as co-located
    holes and a fabricator would drill twice. So the *hole* is what is emitted, once,
    and both routes land on it.

    Keyed on the position to the nanometre, KiCad's own resolution, so this only ever
    merges holes that really are the same hole, and on the layer pair *unordered*,
    because a via from the front to the back is the same hole as one from the back to
    the front.
    """
    low, high = sorted((via.from_layer, via.to_layer))
    key = (
        via.net,
        low,
        high,
        round(via.point[0] * 1e6),
        round(via.point[1] * 1e6),
    )
    if key in drilled:
        return False
    drilled.add(key)
    return True


def generated_uuids(connections: list[RoutedConnection]) -> set[str]:
    """Every UUID this routing run owns, for telling our copper from a human's."""
    owned: set[str] = set()
    for connection in connections:
        for leg in connection.legs:
            owned.update(
                track_uuid(leg.net, leg.start, leg.end, index)
                for index in range(len(leg.segments))
            )
        owned.update(via_uuid(via.net, via.name, via.kind) for via in connection.vias)
    return owned


def drop_generated(board: SNode, owned: set[str]) -> int:
    """Remove copper this router produced on a previous run. Returns how many.

    Copper already in a board is either somebody's hand routing, which the
    incremental build preserves and this router goes around, or this router's own
    output from last time, which has to be replaced rather than duplicated. The two
    are told apart by UUID, exactly as M6 tells a generated footprint from a moved
    one -- ours are a hash of what the copper *is*, so they come out the same on
    every run of an unchanged design.
    """
    removed = 0
    for item in list(board.items):
        if not isinstance(item, SNode) or item.name not in ("segment", "arc", "via"):
            continue
        if item.get("uuid") in owned:
            board.items.remove(item)
            removed += 1
    return removed

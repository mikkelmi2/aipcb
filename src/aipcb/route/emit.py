"""Turning tightened routes into KiCad track segments.

Rubber-band tightening produces any-angle geometry, and that is what gets emitted:
ordinary ``segment`` items, exactly as KiCad writes them itself. Nothing downstream
-- KiCad, Gerber, the fab -- sees anything unusual (ADR 0006).

Track UUIDs are derived from the route they belong to and the segment's position
along it, so they are stable across rebuilds. That is what lets an incremental build
tell its own copper apart from a human's.
"""

from __future__ import annotations

from aipcb.ids import element_uuid, net_codes
from aipcb.kicad.sexpr import SNode, num, quoted, sym
from aipcb.route.stretch import StretchResult

__all__ = ["attach_tracks", "generated_track_uuids", "segments_for", "track_uuid"]


def track_uuid(net: str, start: str, end: str, index: int) -> str:
    """The stable identity of one track segment."""
    return element_uuid("track", net, f"{start}>{end}", index)


def segments_for(
    result: StretchResult, start: str, end: str, net_code: int
) -> list[SNode]:
    """The KiCad ``segment`` items for one tightened route."""
    nodes: list[SNode] = []
    for index, (a, b) in enumerate(result.segments):
        nodes.append(
            SNode("segment").add(
                SNode("start").add(num(a[0]), num(a[1])),
                SNode("end").add(num(b[0]), num(b[1])),
                SNode("width").add(num(result.width)),
                SNode("layer").add(quoted(result.layer)),
                SNode("net").add(sym(str(net_code))),
                SNode("uuid").add(quoted(track_uuid(result.net, start, end, index))),
            )
        )
    return nodes


def attach_tracks(
    board: SNode,
    routed: list[tuple[StretchResult, str, str]],
    nets: list[str],
) -> int:
    """Add every routed segment to a board. Returns how many were added."""
    codes = net_codes(nets)
    added = 0
    # Sorted so the file's order depends on the design, never on routing order.
    for result, start, end in sorted(routed, key=lambda r: (r[0].net, r[1], r[2])):
        code = codes.get(result.net)
        if code is None:
            continue
        for node in segments_for(result, start, end, code):
            board.add(node)
            added += 1
    return added


def generated_track_uuids(
    routed: list[tuple[StretchResult, str, str]],
) -> set[str]:
    """Every UUID this routing run owns, for telling our copper from a human's."""
    return {
        track_uuid(result.net, start, end, index)
        for result, start, end in routed
        for index in range(len(result.segments))
    }

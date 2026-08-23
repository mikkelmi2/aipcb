# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Build a design and route it -- the whole of ``aipcb route all``, minus the CLI.

Extracted so that more than one command can produce a routed board. ``aipcb
simulate`` needs one and cannot use ``aipcb export``, which builds into a throwaway
directory and therefore ships a board with no copper on it at all (ADR 0011, gap 10).

This is a move, not a rewrite: the order of operations below is exactly what
``route_all`` did before, because that order is load-bearing. Dropping the previous
run's stitching before the router looks at the board, and routing once with manual
copper ignored to learn which UUIDs are ours, are both things that decide whether a
second run reproduces the first byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aipcb.compile.build import BuildResult, build_design
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SNode, dump, parse
from aipcb.route.manual import RoutingStates
from aipcb.route.plan import DEFAULT_CONGESTION, RoutedBoard
from aipcb.route.stitch import StitchResult

__all__ = ["RoutedDesign", "build_and_parse", "route_design"]


@dataclass(slots=True)
class RoutedDesign:
    """A built, routed board and everything the caller needs to talk about it."""

    build: BuildResult
    board_path: Path
    board: SNode
    routed: RoutedBoard
    stitched: StitchResult
    segments: int
    vias: int

    def states(self) -> RoutingStates:
        """Every net's routing state, read off the board this run just wrote.

        Read off the board rather than assembled from what the router did, because a
        declared-manual net's copper may have arrived by a route this program knows
        nothing about -- a hand route in KiCad, or a session file from an external
        router (M14d).
        """
        from aipcb.route.manual import routing_states

        return routing_states(
            self.board,
            self.build.netlist,
            auto_routed={c.net for c in self.routed.connections},
            handed_over={f.net: f.reason for f in self.routed.failed},
        )


def build_and_parse(
    design: Path, out: Path, report: Report
) -> tuple[BuildResult, Path, SNode]:
    """Build a design and hand back the parsed board alongside it."""
    result = build_design(design, out_dir=out, report=report)
    board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    return result, board_path, parse(board_path.read_text(encoding="utf-8"))


def route_design(
    design: Path,
    out: Path,
    report: Report,
    *,
    layers: tuple[str, ...] | None = None,
    congestion: float = DEFAULT_CONGESTION,
) -> RoutedDesign:
    """Build ``design`` into ``out``, route it, and write the tracks into the board."""
    from aipcb.route.emit import attach_copper, drop_generated, generated_uuids
    from aipcb.route.plan import route_board
    from aipcb.route.stitch import stitch_board, stitch_uuids
    from aipcb.route.transition import transition_uuids

    result, board_path, board = build_and_parse(design, out, report)
    topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()

    def run(manual_copper: bool) -> RoutedBoard:
        return route_board(
            board,
            result.netlist,
            report,
            layers=layers,
            topologies=topologies,
            congestion=congestion,
            manual_copper=manual_copper,
        )

    # Copper already in the board is either somebody's hand routing, which must
    # be preserved and routed around, or this command's own output from a
    # previous run, which must be replaced rather than duplicated. Routing once
    # while ignoring all of it says which UUIDs *we* would produce, and anything
    # in the board carrying one of those is ours. Only run when there is copper
    # to sort out, which on a first build there is not.
    #
    # Last run's stitching vias go first, and before the router looks at the
    # board at all: they are ours too, and leaving them in would make this run's
    # routing depend on the previous run's stitching, which is the end of
    # byte-stability.
    drop_generated(board, stitch_uuids(result.netlist))
    drop_generated(board, transition_uuids(result.netlist))
    if list(board.children("segment")) or list(board.children("via")):
        owned = generated_uuids(run(False).connections)
        drop_generated(board, owned)
    routed = run(True)
    count, via_count = attach_copper(
        board, routed.connections, sorted(result.netlist.nets)
    )
    stitched = stitch_board(board, result.netlist, report)
    board_path.write_text(dump(board), encoding="utf-8")
    return RoutedDesign(
        build=result,
        board_path=board_path,
        board=board,
        routed=routed,
        stitched=stitched,
        segments=count,
        vias=via_count,
    )

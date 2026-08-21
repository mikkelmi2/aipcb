"""The check loop: build, run KiCad's checks, report against the source.

This is the operation an agent runs after every edit. It exists to make one thing
true: *everything the toolchain knows about a design comes back in one shape*.
Whether a problem was found by our schema validator, by our semantic checks, or by
KiCad's DRC, it arrives as a diagnostic with a file, a line, a stable code and a
hint.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aipcb.checks.highspeed import (
    HighSpeedReport,
    analyse_highspeed,
    report_highspeed,
)
from aipcb.checks.kicad_reports import CheckOutcome, run_drc, run_erc
from aipcb.checks.mapping import build_index
from aipcb.checks.planes import PlaneReport, analyse_planes, report_planes
from aipcb.compile.build import BuildResult, build_design
from aipcb.diagnostics import Report
from aipcb.highspeed import controlled_classes
from aipcb.kicad.fill import FillError, FillResult, fill_project
from aipcb.netlist import Netlist
from aipcb.route.plan import RoutedBoard
from aipcb.route.stitch import StitchResult

__all__ = ["CheckResult", "check_design"]


@dataclass(slots=True)
class CheckResult:
    """Everything one check run produced."""

    netlist: Netlist
    build: BuildResult
    erc: CheckOutcome = field(default_factory=CheckOutcome)
    drc: CheckOutcome = field(default_factory=CheckOutcome)
    routing: RoutedBoard | None = None
    """What the router made of the board, when the check routed it."""
    stitching: StitchResult | None = None
    """The stitching vias generated after routing and before the fill."""
    fill: FillResult | None = None
    """What KiCad's zone filler produced, when the design declares pours."""
    planes: list[PlaneReport] = field(default_factory=list)
    """Plane integrity, measured off the filled board (M10d)."""
    highspeed: HighSpeedReport | None = None
    """The high-speed verification report, when the design declares one (M11e)."""
    filled_board: Path | None = None
    """The staged, filled copy DRC actually ran against."""

    def summary(self) -> dict[str, Any]:
        out = {
            "design": self.netlist.name,
            "revision": self.netlist.revision,
            **self.netlist.stats(),
            "erc": {"ran": self.erc.ran, **self.erc.counts},
            "drc": {"ran": self.drc.ran, **self.drc.counts},
        }
        if self.routing is not None:
            out["routing"] = self.routing.summary()
        if self.stitching is not None:
            out["stitching"] = self.stitching.summary()
        if self.fill is not None:
            out["fill"] = self.fill.to_dict()
        if self.planes:
            out["planes"] = [plane.to_dict() for plane in self.planes]
        if self.highspeed is not None:
            out["highspeed"] = self.highspeed.to_dict()
        return out

    @property
    def handed_over(self) -> list[dict[str, Any]]:
        """Nets the router refused, for an agent to react to."""
        return self.routing.handed_over() if self.routing is not None else []


def check_design(
    design_path: Path,
    *,
    out_dir: Path | None = None,
    report: Report | None = None,
    schematic: bool = True,
    board: bool = True,
    route: bool = True,
) -> CheckResult:
    """Build a design, route it, and run KiCad's checks against the result.

    Output goes to ``out_dir`` when given, and to a temporary directory otherwise,
    so checking a design never leaves artefacts beside the source unless asked.

    Routing before DRC is deliberate. A check that reports zero violations on a
    board with no copper on it has checked almost nothing, and the question an agent
    actually has -- "is this design buildable" -- is not answerable without trying.
    What the router refuses comes back as a machine-readable hand-over list rather
    than as marginal geometry, so the DRC result stays meaningful: zero violations on
    whatever *is* routed, plus an explicit account of what is not.
    """
    report = report if report is not None else Report()

    if out_dir is not None:
        return _check_into(design_path, out_dir, report, schematic, board, route)
    with tempfile.TemporaryDirectory(prefix="aipcb-check-") as tmp:
        return _check_into(design_path, Path(tmp), report, schematic, board, route)


def _check_into(
    design_path: Path,
    target: Path,
    report: Report,
    schematic: bool,
    board: bool,
    route: bool,
) -> CheckResult:
    build = build_design(design_path, out_dir=target, report=report)
    index = build_index(build.netlist)
    result = CheckResult(netlist=build.netlist, build=build)

    board_path = next((p for p in build.written if p.suffix == ".kicad_pcb"), None)
    if route and board_path is not None:
        result.routing, result.stitching = _route_in_place(
            board_path, build.netlist, report
        )

    if schematic:
        path = next((p for p in build.written if p.suffix == ".kicad_sch"), None)
        if path is not None:
            result.erc = run_erc(path, index, report, work=target)
    if board and board_path is not None:
        checked = _fill_for_checking(board_path, build.netlist, target, report, result)
        if checked is None:
            return result
        result.drc = run_drc(checked, index, report, work=target)
        _measure_planes(checked, build.netlist, report, result)
        _measure_highspeed(checked, build.netlist, report, result)
    return result


def _fill_for_checking(
    board_path: Path,
    netlist: Netlist,
    target: Path,
    report: Report,
    result: CheckResult,
) -> Path | None:
    """Fill the zones into a staged copy, and hand back the board DRC should read.

    DRC over an unfilled pour checks nothing about the pour -- KiCad plots and
    checks the fill data that is in the file and never regenerates it (ADR 0009,
    Finding 1) -- so a board with pours is filled first, in a copy, leaving the
    build output as the unfilled reference.

    A fill that fails stops the check. It is tempting to fall back to the unfilled
    board and carry on, and that is precisely the silent corruption this must not
    do: an unfilled pour looks exactly like a filled one to every downstream tool
    and exports as no copper at all.
    """
    if not netlist.pours:
        return board_path
    try:
        filled, outcome = fill_project(
            board_path, target / "filled", measure_islands=True
        )
    except FillError as exc:
        report.error(
            "zone-fill-failed",
            f"the zones could not be filled, so DRC was not run: {exc.message}",
            hint=exc.detail or "an unfilled pour exports as no copper at all, so "
            "checking one would report a clean board that is not one",
        )
        return None
    result.fill = outcome
    result.filled_board = filled
    return filled


def _measure_highspeed(
    board_path: Path,
    netlist: Netlist,
    report: Report,
    result: CheckResult,
) -> None:
    """Project every controlled-impedance net onto its reference plane (M11e).

    Reads the same filled board DRC ran against, and the pair measurements the
    router made while it routed. Nothing here fills anything: M10 already paid for
    that, and the staged copy is right there.
    """
    from aipcb.kicad.sexpr import SExprError, parse

    if not controlled_classes(netlist):
        return
    try:
        tree = parse(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError):  # pragma: no cover
        return
    audits = result.routing.pair_audits if result.routing is not None else []
    skew = result.routing.skew if result.routing is not None else {}
    result.highspeed = analyse_highspeed(
        tree, netlist, audits, skew, filled=result.filled_board is not None
    )
    report_highspeed(result.highspeed, netlist, report)


def _measure_planes(
    board_path: Path, netlist: Netlist, report: Report, result: CheckResult
) -> None:
    """Read the filled zones back and report what the fill actually produced."""
    from aipcb.kicad.sexpr import SExprError, parse

    if not netlist.pours or result.fill is None:
        return
    try:
        tree = parse(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError):  # pragma: no cover - just written
        return
    removed = {
        zone.uuid: (zone.islands_removed, zone.area_removed_mm2)
        for zone in result.fill.per_zone
    }
    result.planes = analyse_planes(tree, netlist, removed=removed)
    report_planes(result.planes, netlist, report)


def _route_in_place(
    board_path: Path, netlist: Netlist, report: Report
) -> tuple[RoutedBoard | None, StitchResult | None]:
    """Route the freshly built board, stitch it, and write the copper back into it.

    Stitching runs after routing and before the fill, which is the only ordering
    that works: the tracks are what a via position has to avoid, and the fill is
    what turns a via into part of the plane rather than isolated copper.
    """
    from aipcb.kicad.sexpr import SExprError, dump, parse
    from aipcb.route.emit import attach_copper
    from aipcb.route.plan import route_board
    from aipcb.route.stitch import stitch_board

    try:
        tree = parse(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError):  # pragma: no cover - just built
        return None, None
    topologies = tuple(netlist.layout.routes) if netlist.layout else ()
    routed = route_board(tree, netlist, report, topologies=topologies)
    attach_copper(tree, routed.connections, sorted(netlist.nets))
    stitched = stitch_board(tree, netlist, report)
    board_path.write_text(dump(tree), encoding="utf-8")
    return routed, stitched

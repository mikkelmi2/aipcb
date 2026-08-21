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

from aipcb.checks.kicad_reports import CheckOutcome, run_drc, run_erc
from aipcb.checks.mapping import build_index
from aipcb.compile.build import BuildResult, build_design
from aipcb.diagnostics import Report
from aipcb.netlist import Netlist
from aipcb.route.plan import RoutedBoard

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
        result.routing = _route_in_place(board_path, build.netlist, report)

    if schematic:
        path = next((p for p in build.written if p.suffix == ".kicad_sch"), None)
        if path is not None:
            result.erc = run_erc(path, index, report, work=target)
    if board and board_path is not None:
        result.drc = run_drc(board_path, index, report, work=target)
    return result


def _route_in_place(
    board_path: Path, netlist: Netlist, report: Report
) -> RoutedBoard | None:
    """Route the freshly built board and write the copper back into it."""
    from aipcb.kicad.sexpr import SExprError, dump, parse
    from aipcb.route.emit import attach_copper
    from aipcb.route.plan import route_board

    try:
        tree = parse(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError):  # pragma: no cover - just built
        return None
    topologies = tuple(netlist.layout.routes) if netlist.layout else ()
    routed = route_board(tree, netlist, report, topologies=topologies)
    attach_copper(tree, routed.connections, sorted(netlist.nets))
    board_path.write_text(dump(tree), encoding="utf-8")
    return routed

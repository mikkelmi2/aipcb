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

__all__ = ["CheckResult", "check_design"]


@dataclass(slots=True)
class CheckResult:
    """Everything one check run produced."""

    netlist: Netlist
    build: BuildResult
    erc: CheckOutcome = field(default_factory=CheckOutcome)
    drc: CheckOutcome = field(default_factory=CheckOutcome)

    def summary(self) -> dict[str, Any]:
        return {
            "design": self.netlist.name,
            "revision": self.netlist.revision,
            **self.netlist.stats(),
            "erc": {"ran": self.erc.ran, **self.erc.counts},
            "drc": {"ran": self.drc.ran, **self.drc.counts},
        }


def check_design(
    design_path: Path,
    *,
    out_dir: Path | None = None,
    report: Report | None = None,
    schematic: bool = True,
    board: bool = True,
) -> CheckResult:
    """Build a design and run KiCad's checks against the result.

    Output goes to ``out_dir`` when given, and to a temporary directory otherwise,
    so checking a design never leaves artefacts beside the source unless asked.
    """
    report = report if report is not None else Report()

    if out_dir is not None:
        return _check_into(design_path, out_dir, report, schematic, board)
    with tempfile.TemporaryDirectory(prefix="aipcb-check-") as tmp:
        return _check_into(design_path, Path(tmp), report, schematic, board)


def _check_into(
    design_path: Path, target: Path, report: Report, schematic: bool, board: bool
) -> CheckResult:
    build = build_design(design_path, out_dir=target, report=report)
    index = build_index(build.netlist)
    result = CheckResult(netlist=build.netlist, build=build)

    if schematic:
        path = next((p for p in build.written if p.suffix == ".kicad_sch"), None)
        if path is not None:
            result.erc = run_erc(path, index, report, work=target)
    if board:
        path = next((p for p in build.written if p.suffix == ".kicad_pcb"), None)
        if path is not None:
            result.drc = run_drc(path, index, report, work=target)
    return result

"""The build pipeline: source in, KiCad files out.

One place decides the order of operations -- load, elaborate, check, emit -- so the
CLI, the tests and later milestones all get the same behaviour. Nothing is written
if validation found errors: a KiCad file compiled from a design with an unresolved
part would be quietly wrong, and quietly wrong output is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aipcb.checks.kicad_bindings import check_kicad_bindings
from aipcb.checks.semantic import run_semantic_checks
from aipcb.compile.project import (
    build_fp_lib_table,
    build_project,
    build_sym_lib_table,
    render_project,
)
from aipcb.compile.schematic import build_schematic
from aipcb.diagnostics import AipcbError, Report
from aipcb.elaborate import elaborate
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import dump
from aipcb.loader import load_design
from aipcb.netlist import Netlist

__all__ = ["BuildResult", "build_design", "compile_netlist"]


@dataclass(slots=True)
class BuildResult:
    """What a build produced."""

    netlist: Netlist
    written: list[Path] = field(default_factory=list)
    project: str = ""


def compile_netlist(design_path: Path, report: Report) -> Netlist:
    """Load, elaborate and check a design without writing anything."""
    loaded = load_design(design_path, report=report)
    netlist = elaborate(loaded, report)
    run_semantic_checks(netlist, report)
    check_kicad_bindings(loaded, report)
    return netlist


def build_design(
    design_path: Path,
    *,
    out_dir: Path | None = None,
    report: Report | None = None,
) -> BuildResult:
    """Compile a design to KiCad files.

    Raises :class:`~aipcb.diagnostics.AipcbError` if validation failed, carrying the
    report so the caller can present it.
    """
    report = report if report is not None else Report()
    netlist = compile_netlist(design_path, report)

    if not report.ok:
        raise AipcbError(
            f"{design_path}: not building, because validation found "
            f"{len(report.errors)} error{'s' if len(report.errors) != 1 else ''}",
            report,
        )

    target = out_dir or design_path.parent
    target.mkdir(parents=True, exist_ok=True)
    project = _project_name(netlist.name)

    written: list[Path] = []

    sheet_uuid = element_uuid("sheet", "/")
    project_path = target / f"{project}.kicad_pro"
    _write_if_changed(
        project_path,
        render_project(
            build_project(
                project,
                sheet_uuid,
                netlist.net_classes,
                {n.name: n.net_class for n in netlist.nets.values()},
            )
        ),
    )
    written.append(project_path)

    schematic_path = target / f"{project}.kicad_sch"
    _write_if_changed(schematic_path, dump(build_schematic(netlist, project=project)))
    written.append(schematic_path)

    symbol_libs = {
        c.part.symbol.partition(":")[0]
        for c in netlist.components.values()
        if c.part is not None
    }
    symbol_libs.add("power")  # PWR_FLAG, and any power symbols we place
    footprint_libs = {
        c.part.footprint.partition(":")[0]
        for c in netlist.components.values()
        if c.part is not None
    }

    sym_table = target / "sym-lib-table"
    _write_if_changed(sym_table, dump(build_sym_lib_table(symbol_libs)))
    written.append(sym_table)

    fp_table = target / "fp-lib-table"
    _write_if_changed(fp_table, dump(build_fp_lib_table(footprint_libs)))
    written.append(fp_table)

    return BuildResult(netlist=netlist, written=written, project=project)


def _project_name(design_name: str) -> str:
    """A filesystem-safe project name. KiCad expects the files to share a stem."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in design_name)


def _write_if_changed(path: Path, text: str) -> bool:
    """Write only when the content differs, so unchanged files keep their mtime.

    Builds are deterministic, so an unchanged design produces identical bytes;
    leaving the file alone keeps incremental tooling and file watchers quiet.
    """
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True

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
from aipcb.compile.board import build_board
from aipcb.compile.preserve import (
    MergeStats,
    merge_board,
    schematic_was_edited,
    stamp_schematic,
)
from aipcb.compile.project import (
    build_fp_lib_table,
    build_project,
    build_sym_lib_table,
    render_project,
)
from aipcb.compile.review import ReviewResult
from aipcb.compile.schematic import build_schematic
from aipcb.diagnostics import AipcbError, Report
from aipcb.elaborate import elaborate
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SExprError, SNode, dump, parse
from aipcb.loader import load_design
from aipcb.netlist import Netlist

__all__ = ["BuildResult", "build_design", "compile_netlist", "project_name"]


@dataclass(slots=True)
class BuildResult:
    """What a build produced."""

    netlist: Netlist
    written: list[Path] = field(default_factory=list)
    project: str = ""
    merge: MergeStats | None = None
    """What was preserved from an existing board, when one was there."""
    review: ReviewResult | None = None
    """The rendered review artefacts, when ``--render`` asked for them."""


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
    fresh: bool = False,
    render: bool = False,
) -> BuildResult:
    """Compile a design to KiCad files.

    By default the build is *incremental*: an existing board is read first and the
    parts of it a human edited are folded into the new one. Pass ``fresh=True`` to
    regenerate from source and discard those edits.

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
    project = project_name(netlist.name)

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
                edge_clearance=netlist.board.edge_clearance if netlist.board else None,
            )
        ),
    )
    written.append(project_path)

    schematic_path = target / f"{project}.kicad_sch"
    schematic = stamp_schematic(build_schematic(netlist, project=project))
    _report_schematic_edits(schematic_path, schematic, report)
    _write_if_changed(schematic_path, dump(schematic))
    written.append(schematic_path)

    board_path = target / f"{project}.kicad_pcb"
    board = build_board(netlist, project=project, report=report)
    merge_stats: MergeStats | None = None
    if not fresh:
        existing = _read_board(board_path, report)
        if existing is not None:
            board, merge_stats = merge_board(board, existing, netlist, report)
    _write_if_changed(board_path, dump(board))
    written.append(board_path)

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

    review: ReviewResult | None = None
    if render:
        from aipcb.compile.review import render_review

        review = render_review(schematic_path, target, netlist, report)
        written.extend(review.files)

    return BuildResult(
        netlist=netlist,
        written=written,
        project=project,
        merge=merge_stats,
        review=review,
    )


def _report_schematic_edits(path: Path, fresh: SNode, report: Report) -> None:
    """Say out loud that a hand-edited sheet is about to be regenerated.

    ADR 0003 (amended by M14) makes the schematic a *view* of the source rather than
    a document: everything on it -- every symbol, wire, label and power symbol -- is
    generated, so there is nothing on the sheet that a rebuild could sensibly hand
    back. The board is the opposite case, and M6 preserves it accordingly.

    What that policy owes the person who moved a symbol is a sentence, not silence.
    A sheet whose stamp no longer matches its contents has been edited, and this says
    so before the edit is overwritten -- and says where the edit belongs instead.
    """
    if not path.exists():
        return
    try:
        existing = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError):
        return  # unreadable: the write below replaces it, and says nothing it cannot back up
    if dump(existing) == dump(fresh):
        return
    edited = schematic_was_edited(existing)
    if edited is not True:
        return
    report.warning(
        "schematic-edits-discarded",
        f"{path.name} has been edited since aipcb generated it, and those edits are "
        "about to be regenerated away",
        hint="the schematic is a view of the design, not a second copy of it: move "
        "the change into the YAML, where it is reviewable and survives. Copy the "
        "sheet aside first if you want to keep it",
    )


def _read_board(path: Path, report: Report) -> SNode | None:
    """Parse an existing board, or explain why it could not be used.

    A board we cannot parse is not silently overwritten: whatever is in it might be
    hours of somebody's routing, and the right response is to say so rather than to
    quietly replace it.
    """
    if not path.exists():
        return None
    try:
        return parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        report.warning(
            "existing-board-unreadable",
            f"could not read the existing board at {path}: {exc}",
            hint="it will be replaced; move it aside first if it holds work you want",
        )
    except SExprError as exc:
        report.warning(
            "existing-board-unparseable",
            f"the existing board at {path} is not valid KiCad syntax: {exc}",
            hint="it will be replaced; move it aside first if it holds work you want",
        )
    return None


def project_name(design_name: str) -> str:
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

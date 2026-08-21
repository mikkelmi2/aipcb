"""Fabrication output: Gerbers, drill files, and the rest of what a fab needs.

This closes the path the project set out to build. Source goes in one end, and what
comes out the other is a package a board house can quote from -- with no step in
between that required opening a GUI.

Everything is produced by ``kicad-cli``, so the files are byte-for-byte what KiCad
itself would plot. We choose the layer set and the options; KiCad does the plotting.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.diagnostics import Report
from aipcb.kicad.cli import KicadCliMissing, run_kicad
from aipcb.kicad.fill import FillError, fill_project
from aipcb.netlist import Netlist

__all__ = ["ExportResult", "export_board", "gerber_layers", "position_file_name"]

#: Technical layers every board needs plotted, whatever its copper count.
_TECHNICAL = (
    "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "Edge.Cuts",
)


def position_file_name(board: Path) -> str:
    """What KiCad itself calls a both-sides placement file.

    KiCad 9 builds this name in ``DIALOG_GEN_FOOTPRINT_POSITION``: the board name,
    then ``PLACE_FILE_EXPORTER::DecorateFilename`` appends ``-all`` when one file
    carries both sides (``-top`` / ``-bottom`` otherwise), then the CSV branch
    appends ``-`` and ``FILEEXT::FootprintPlaceFileExtension`` -- which is ``pos``
    -- and sets the extension to ``csv``.

    ``kicad-cli pcb export pos`` has no such convention of its own: with no
    ``--output`` it writes plain ``<board>.csv``, because it just swaps the
    extension. So the name is ours to choose, and the ecosystem convention is the
    one worth matching -- every tool that consumes a KiCad placement file looks for
    ``*pos.csv``, and a name outside that pattern is not rejected, it is simply
    never found.
    """
    return f"{board.stem}-all-pos.csv"


def gerber_layers(copper_layers: int) -> list[str]:
    """The layer list to plot, front to back."""
    copper = ["F.Cu"]
    copper += [f"In{i}.Cu" for i in range(1, max(copper_layers - 1, 1))]
    copper.append("B.Cu")
    return [*copper, *_TECHNICAL]


@dataclass(slots=True)
class ExportResult:
    """What an export produced."""

    directory: Path
    files: list[Path] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    ok: bool = True

    def by_suffix(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for path in self.files:
            counts[path.suffix] = counts.get(path.suffix, 0) + 1
        return dict(sorted(counts.items()))


def export_board(
    board: Path,
    out_dir: Path,
    netlist: Netlist,
    report: Report,
    *,
    schematic: Path | None = None,
    position: bool = True,
) -> ExportResult:
    """Plot Gerbers and drill files, and optionally a BOM and placement file.

    A board with pours is filled first, in a staged copy. ``kicad-cli pcb export
    gerbers`` plots whatever fill data is in the file and never regenerates it
    (ADR 0009, Finding 1), so plotting an unfilled pour ships a Gerber with no
    plane copper on it at all -- silently, and looking exactly like a correct one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult(directory=out_dir)

    if netlist.pours:
        with tempfile.TemporaryDirectory(prefix="aipcb-fill-") as staging:
            return _export_filled(
                board, Path(staging), out_dir, netlist, report, result,
                schematic=schematic, position=position,
            )
    return _plot(board, out_dir, netlist, report, result,
                 schematic=schematic, position=position)


def _export_filled(
    board: Path,
    staging: Path,
    out_dir: Path,
    netlist: Netlist,
    report: Report,
    result: ExportResult,
    *,
    schematic: Path | None,
    position: bool,
) -> ExportResult:
    """Fill into ``staging`` and plot from there, so build output stays unfilled."""
    try:
        filled, outcome = fill_project(board, staging)
    except FillError as exc:
        report.error(
            "zone-fill-failed",
            f"the zones could not be filled, so nothing was exported: {exc.message}",
            hint=exc.detail or "Gerbers plotted from an unfilled pour carry no plane "
            "copper at all, which is worse than no Gerbers",
        )
        result.ok = False
        return result
    result.steps.append(f"fill ({outcome.filled}/{outcome.zones} zones)")
    plotted = _plot(filled, out_dir, netlist, report, result,
                    schematic=schematic, position=position)
    # The staged copy is about to vanish with the temporary directory; the files
    # that matter are already in `out_dir`.
    plotted.files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    return plotted


def _plot(
    board: Path,
    out_dir: Path,
    netlist: Netlist,
    report: Report,
    result: ExportResult,
    *,
    schematic: Path | None,
    position: bool,
) -> ExportResult:
    """Run every ``kicad-cli`` plot step against one board."""
    copper_layers = netlist.layout.stackup.copper_layers if netlist.layout else 2
    layers = ",".join(gerber_layers(copper_layers))

    steps: list[tuple[str, tuple[str, ...]]] = [
        (
            "gerbers",
            (
                "pcb", "export", "gerbers",
                "--layers", layers,
                "--no-protel-ext",
                "--use-drill-file-origin",
                "-o", str(out_dir), str(board),
            ),
        ),
        (
            "drill",
            (
                "pcb", "export", "drill",
                "--format", "excellon",
                "--drill-origin", "plot",
                "--excellon-units", "mm",
                "--generate-map", "--map-format", "gerberx2",
                "-o", str(out_dir) + "/", str(board),
            ),
        ),
    ]
    if position:
        steps.append(
            (
                "position",
                (
                    "pcb", "export", "pos",
                    "--format", "csv", "--units", "mm", "--side", "both",
                    "-o", str(out_dir / position_file_name(board)), str(board),
                ),
            )
        )

    for name, args in steps:
        try:
            run = run_kicad(*args)
        except KicadCliMissing as exc:
            report.error("kicad-cli-missing", str(exc).splitlines()[0], hint=str(exc))
            result.ok = False
            return result
        if run.returncode != 0:
            detail = (run.stderr or run.stdout).strip().splitlines()
            report.error(
                f"export-{name}-failed",
                f"kicad-cli could not export {name}: "
                f"{detail[-1] if detail else 'no output'}",
                hint=f"command was: {run.command}",
            )
            result.ok = False
        else:
            result.steps.append(name)

    if schematic is not None:
        _export_bom(schematic, out_dir, report, result)

    result.files = sorted(p for p in out_dir.rglob("*") if p.is_file())
    return result


def _export_bom(schematic: Path, out_dir: Path, report: Report, result: ExportResult) -> None:
    """A bill of materials, so the parts list ships alongside the fabrication data.

    Taken from the schematic rather than the board, because that is where KiCad
    keeps the fields a BOM needs -- and because the schematic is what carries the
    part's value and datasheet.
    """
    target = out_dir / "bom.csv"
    try:
        run = run_kicad(
            "sch", "export", "bom",
            "--fields", "Reference,Value,Footprint,${QUANTITY},Description",
            "--group-by", "Value,Footprint",
            "-o", str(target), str(schematic),
        )
    except KicadCliMissing:
        return
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip().splitlines()
        report.warning(
            "export-bom-failed",
            f"could not export the BOM: {detail[-1] if detail else 'no output'}",
            hint="the Gerbers and drill files are unaffected",
        )
        return
    result.steps.append("bom")

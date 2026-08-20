"""Fabrication output: Gerbers, drill files, and the rest of what a fab needs.

This closes the path the project set out to build. Source goes in one end, and what
comes out the other is a package a board house can quote from -- with no step in
between that required opening a GUI.

Everything is produced by ``kicad-cli``, so the files are byte-for-byte what KiCad
itself would plot. We choose the layer set and the options; KiCad does the plotting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aipcb.diagnostics import Report
from aipcb.kicad.cli import KicadCliMissing, run_kicad
from aipcb.netlist import Netlist

__all__ = ["ExportResult", "export_board", "gerber_layers"]

#: Technical layers every board needs plotted, whatever its copper count.
_TECHNICAL = (
    "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "Edge.Cuts",
)


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
    """Plot Gerbers and drill files, and optionally a BOM and placement file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = ExportResult(directory=out_dir)

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
                    "-o", str(out_dir / "positions.csv"), str(board),
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

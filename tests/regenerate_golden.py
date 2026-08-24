# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the golden KiCad files.

Run this after an intentional change to the emitters, then read the diff: it shows
exactly what every design's output gains or loses. Run from the repository root:

    .venv/bin/python -m tests.regenerate_golden

`examples/pcie-sata` additionally gets its **assembly package** written to
`tests/golden/pcie-sata/assembly/` -- the bill of materials and the centroid file an
assembler is sent, plus the placement overlay a human checks them against (M21d).
That step needs `kicad-cli` for the placement file and is skipped without it, with a
note; the KiCad files above are regenerated either way.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from aipcb.compile.build import build_design

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
KEEP = {".kicad_sch", ".kicad_pcb", ".kicad_pro"}

#: The board whose assembly package is committed and reviewed. One is enough: the
#: package is a function of the placement, and what a golden catches here is a
#: change to the *format* rather than to any one design.
ASSEMBLY_EXAMPLE = "pcie-sata"

#: Committed for review rather than every format: the two files JLCPCB is actually
#: sent, the complete generic bill for anything they do not carry, and the overlay.
ASSEMBLY_FORMATS = ("jlcpcb", "generic")


def main() -> int:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for design in sorted(ROOT.glob("examples/*/design.yaml")):
        name = design.parent.name
        with tempfile.TemporaryDirectory() as tmp:
            result = build_design(design, out_dir=Path(tmp))
            target = GOLDEN / name
            target.mkdir(parents=True, exist_ok=True)
            for path in result.written:
                if path.suffix in KEEP:
                    shutil.copy2(path, target / path.name)
                    print(f"wrote {target / path.name}")
            if name == ASSEMBLY_EXAMPLE:
                _assembly(design, GOLDEN / name / "assembly")
    return 0


def _assembly(design: Path, target: Path) -> None:
    """The assembly package, regenerated beside the KiCad files."""
    from aipcb.compile.export import export_board
    from aipcb.diagnostics import Report
    from aipcb.kicad.cli import find_kicad_cli

    if find_kicad_cli() is None:
        print(f"skipped {target}: kicad-cli is not on PATH")
        return
    report = Report()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        result = build_design(design, out_dir=work / "build", report=report)
        board = next(p for p in result.written if p.suffix == ".kicad_pcb")
        sch = next((p for p in result.written if p.suffix == ".kicad_sch"), None)
        export_board(
            board, work / "out", result.netlist, report, schematic=sch,
            assembly_formats=ASSEMBLY_FORMATS,
        )
        produced = work / "out" / "assembly"
        if not produced.is_dir():
            print(f"skipped {target}: no assembly package was produced")
            return
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(produced.iterdir()):
            if path.is_file():
                shutil.copy2(path, target / path.name)
                print(f"wrote {target / path.name}")


if __name__ == "__main__":
    sys.exit(main())

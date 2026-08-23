# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the golden KiCad files.

Run this after an intentional change to the emitters, then read the diff: it shows
exactly what every design's output gains or loses. Run from the repository root:

    .venv/bin/python -m tests.regenerate_golden
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

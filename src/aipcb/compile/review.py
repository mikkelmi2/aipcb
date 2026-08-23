# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Rendering the artefacts a human reviews.

A ``.kicad_sch`` is not something anybody reads. The thing a reviewer sits down
with -- to check a board against a controller's reference design before it goes to
fab -- is a plotted sheet, and until M14 producing one meant opening KiCad. This
module makes it a build step: ``aipcb build --render`` writes a PDF and an SVG of
every sheet into ``review/`` beside the KiCad files, plus the readability numbers
that say whether the drawing got better or worse.

The renders come from ``kicad-cli sch export``, so they are exactly what KiCad
itself would plot -- this module chooses the arguments and the destination and
nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.compile.readability import Metrics, measure_schematic
from aipcb.compile.sheet import POWER_CLASSES
from aipcb.diagnostics import Report
from aipcb.kicad.cli import find_kicad_cli, run_kicad
from aipcb.kicad.sexpr import parse
from aipcb.netlist import Netlist

__all__ = ["REVIEW_DIR", "ReviewResult", "decoupling_hosts", "render_review"]

#: Where the rendered sheets go, relative to the build output directory.
REVIEW_DIR = "review"

#: The roles whose distance to their host is worth measuring: a capacitor that is
#: there to hold a rail up at a particular pin, and is useless a sheet away from it.
_LOCAL_ROLES = frozenset({"decoupling", "bypass", "bulk"})


@dataclass(slots=True)
class ReviewResult:
    """What a render produced."""

    directory: Path
    files: list[Path] = field(default_factory=list)
    metrics: Metrics | None = None
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "directory": str(self.directory),
            "files": [str(p) for p in self.files],
        }
        if self.metrics is not None:
            payload["readability"] = self.metrics.to_dict()
        if self.skipped:
            payload["skipped"] = self.skipped
        return payload


def decoupling_hosts(netlist: Netlist) -> dict[str, str]:
    """Which local capacitor serves which component, by reference designator.

    Reuses the sheet planner's own resolution, so the number reported is the
    distance from the cap to the IC the *placer* thought it belonged to -- not to
    some second opinion that could quietly disagree with the drawing.
    """
    from aipcb.compile.sheet import _satellites

    power = frozenset(
        n.name for n in netlist.sorted_nets() if n.net_class in POWER_CLASSES
    )
    return {
        refdes: host
        for refdes, host in _satellites(netlist, power).items()
        if (netlist.components[refdes].role or "") in _LOCAL_ROLES
    }


def render_review(
    schematic: Path, out_dir: Path, netlist: Netlist, report: Report
) -> ReviewResult:
    """Plot a schematic to PDF and SVG, and measure it.

    A missing ``kicad-cli`` is reported and skipped rather than raised: the renders
    are review material, and a build that produced correct KiCad files should not
    fail because the machine cannot plot them.
    """
    directory = out_dir / REVIEW_DIR
    result = ReviewResult(directory=directory)

    try:
        root = parse(schematic.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - just written
        result.skipped = f"could not read {schematic}: {exc}"
        return result
    result.metrics = measure_schematic(root, decoupling_hosts(netlist))

    if find_kicad_cli() is None:
        result.skipped = "kicad-cli is not on PATH, so no sheets were plotted"
        report.info(
            "review-render-skipped",
            f"{result.skipped}; the readability measurement still ran",
            hint="install KiCad 8 or 9, or set AIPCB_KICAD_CLI",
        )
        return result

    directory.mkdir(parents=True, exist_ok=True)
    stem = schematic.stem
    for fmt, target in (
        ("pdf", directory / f"{stem}.pdf"),
        ("svg", directory / f"{stem}.svg"),
    ):
        run = run_kicad(
            "sch", "export", fmt, "-o", str(target if fmt == "pdf" else directory),
            str(schematic),
        )
        if not run.ok:
            report.warning(
                "review-render-failed",
                f"kicad-cli could not plot the schematic as {fmt}: "
                f"{run.stderr.strip() or run.stdout.strip()}",
            )
            continue
        produced = target if target.exists() else directory / f"{stem}.{fmt}"
        if produced.exists():
            result.files.append(produced)

    metrics_path = directory / "readability.json"
    metrics_path.write_text(
        json.dumps(result.metrics.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    result.files.append(metrics_path)
    return result

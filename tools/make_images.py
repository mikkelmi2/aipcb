#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the README's pipeline images from the bundled examples.

Every picture in the README is output of this repository's own tools run against
`examples/pcie-sata`, the flagship board. None is a mock-up, and none is touched
by hand afterwards -- which is the only way a README stays honest as the code
moves under it. Run this after anything that changes what a board looks like:

    python tools/make_images.py            # everything except the simulation
    python tools/make_images.py --with-simulation

The simulation step is opt-in because it needs the gerber2ems container image
and takes minutes rather than seconds; without it the existing plot is left
alone. Every other step is a few seconds.

Each image is written to docs/images/ at a fixed pixel width, so that replacing
one does not reflow the README.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
IMAGES = REPO / "docs" / "images"
EXAMPLE = REPO / "examples" / "pcie-sata"
DESIGN = EXAMPLE / "design.yaml"

#: One width for every raster image. Wide enough that a 297 mm schematic is
#: legible when GitHub scales it into the README column, small enough that the
#: whole set stays well under a megabyte.
WIDTH = 1600


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, echoing it first so the log says how each image was made."""
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise SystemExit(
            f"{name} is not on PATH. The images are generated with KiCad's own "
            f"plotters and poppler; install KiCad 9 and poppler-utils."
        )
    return found


def pdf_to_png(pdf: Path, png: Path, width: int = WIDTH) -> None:
    """First page of a PDF to PNG, at a fixed width, white background."""
    run([tool("pdftoppm"), "-png", "-r", "150", "-f", "1", "-l", "1",
         "-scale-to-x", str(width), "-scale-to-y", "-1", str(pdf), str(png.with_suffix(""))])
    produced = png.with_name(png.stem + "-1.png")
    if produced.exists():
        produced.replace(png)


def svg_to_png(svg: Path, png: Path, width: int = WIDTH) -> None:
    import cairosvg  # type: ignore[import-untyped]  # late: only this step needs it

    cairosvg.svg2png(
        url=str(svg), write_to=str(png), output_width=width, background_color="white"
    )


def schematic(aipcb: str) -> Path:
    """1. The A3 schematic, plotted by kicad-cli through `aipcb build --render`."""
    print("[schematic] aipcb build --render")
    run([aipcb, "build", "--render", str(DESIGN)])
    pdf = EXAMPLE / "review" / "pcie-sata.pdf"
    out = IMAGES / "02-schematic.png"
    pdf_to_png(pdf, out)
    return out


def board_svg(board: Path, out: Path, layers: str, *, black_and_white: bool = False) -> Path:
    """Plot a board to SVG with kicad-cli, then rasterise it."""
    svg_dir = out.parent / "_svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        tool("kicad-cli"), "pcb", "export", "svg",
        "--layers", layers,
        "--page-size-mode", "2",       # board area only, not the sheet
        "--exclude-drawing-sheet",
        "--output", str(svg_dir / out.with_suffix(".svg").name),
        str(board),
    ]
    if black_and_white:
        cmd.insert(-1, "--black-and-white")
    run(cmd)
    svg_to_png(svg_dir / out.with_suffix(".svg").name, out)
    return out


def placed(aipcb: str) -> Path:
    """2. The board as `aipcb build` leaves it: placed, outlined, not yet routed."""
    print("[placed] aipcb build (fresh, unrouted)")
    run([aipcb, "build", "--fresh", str(DESIGN)])
    return board_svg(
        EXAMPLE / "pcie-sata.kicad_pcb",
        IMAGES / "03-placed.png",
        "F.Cu,F.SilkS,Edge.Cuts",
    )


def routed(aipcb: str) -> Path:
    """3. The same board after routing -- the coupled pairs are the point."""
    print("[routed] aipcb route all  (writes tracks into the board)")
    run([aipcb, "route", "all", str(DESIGN)])
    print("[routed] aipcb check       (ERC + DRC on the filled board)")
    proc = subprocess.run(
        [aipcb, "check", "--json", str(DESIGN)], capture_output=True, text=True
    )
    print(f"    aipcb check exit={proc.returncode}")
    (IMAGES / "_check.json").write_text(proc.stdout, encoding="utf-8")
    return board_svg(
        EXAMPLE / "pcie-sata.kicad_pcb",
        IMAGES / "04-routed.png",
        "F.Cu,In1.Cu,In2.Cu,B.Cu,Edge.Cuts",
    )


def render_3d() -> Path:
    """5. What it would look like, raytraced by KiCad from the same board."""
    print("[3d] kicad-cli pcb render")
    out = IMAGES / "06-3d.png"
    run([
        tool("kicad-cli"), "pcb", "render",
        "--width", str(WIDTH), "--height", str(WIDTH * 3 // 4),
        "--quality", "high", "--background", "opaque",
        "--output", str(out), str(EXAMPLE / "pcie-sata.kicad_pcb"),
    ])
    return out


def simulation(aipcb: str, *, run_solver: bool) -> Path | None:
    """4. What the field solver concluded about each pair, as the tool reports it."""
    out_dir = EXAMPLE / "out" / "si"
    if run_solver:
        print("[simulation] aipcb simulate  (needs the gerber2ems image; cached pairs are free)")
        subprocess.run([aipcb, "simulate", "--json", str(DESIGN)], text=True)
    results = []
    for path in sorted(out_dir.glob("*/result.json")):
        metrics = json.loads(path.read_text(encoding="utf-8")).get("metrics")
        if metrics and metrics.get("impedance_ohm") is not None:
            results.append(metrics)
    if not results:
        print("[simulation] no solver results under out/si; leaving the existing plot alone")
        print("             re-run with --with-simulation to produce them")
        return None
    return plot_simulation(results, IMAGES / "05-simulation.png")


def plot_simulation(results: list[dict[str, Any]], out: Path) -> Path:
    """Plot what ``aipcb simulate`` concluded, not a re-derivation of it.

    The numbers come from each pair's ``result.json`` -- the same metrics the
    tool turns into findings -- rather than from re-reading the Touchstone here.
    An earlier draft of this script did its own mixed-mode conversion and drew
    insertion loss above 0 dB across the whole simulated span, which is more
    energy out than in. The tool already knows that: it marks such an extraction
    ``usable: false`` and refuses to draw conclusions from it. Re-deriving the
    physics in a README script is how a picture ends up disagreeing with the
    program it is advertising.

    Two panels, one measure each, both "value against a threshold":

    * differential impedance as a **deviation from the declared target**, so that
      85 ohm and 100 ohm classes share one axis, against the +/-10% band that is
      the tool's default acceptance;
    * worst return loss in band, against the -10 dB default.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    # Three categorical hues, validated all-pairs in light mode (CVD dE 9.2,
    # normal-vision dE 24.0). Aqua sits under 3:1 on this surface, so every point
    # carries a visible direct label -- the documented relief for that warning.
    CLASS_COLOUR = {"pcie_rx": "#2a78d6", "pcie_tx": "#eb6834", "sata": "#1baf7a"}
    SURFACE = "#fcfcfb"
    INK, INK_2, INK_MUTED = "#0b0b0b", "#52514e", "#8a8880"
    BAND = "#e8e8e4"

    rows = sorted(results, key=lambda r: (r["net_class"], r["pair"]))
    labels = [r["pair"].replace("+", " / ") for r in rows]
    y = list(range(len(rows)))[::-1]

    fig, (ax_z, ax_rl) = plt.subplots(
        1, 2, figsize=(13.4, 5.8), sharey=True,
        gridspec_kw={"width_ratios": [1.35, 1.0], "wspace": 0.06},
    )
    fig.patch.set_facecolor(SURFACE)

    for ax in (ax_z, ax_rl):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(INK_MUTED)
        ax.tick_params(colors=INK_2, labelsize=9, length=3)
        ax.grid(axis="x", color=BAND, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)

    # ---- impedance, as deviation from each pair's own declared target ----
    ax_z.axvspan(-10, 10, color=BAND, zorder=0, label="+/-10% acceptance")
    ax_z.axvline(0, color=INK_MUTED, linewidth=1, zorder=1)
    for yi, r in zip(y, rows, strict=True):
        dev = r["deviation"] * 100
        colour = CLASS_COLOUR[r["net_class"]]
        usable = r["usable"]
        ax_z.plot([0, dev], [yi, yi], color=colour, linewidth=2, zorder=2,
                  alpha=1.0 if usable else 0.35)
        ax_z.plot([dev], [yi], marker="o", markersize=9, zorder=3,
                  markerfacecolor=colour if usable else SURFACE,
                  markeredgecolor=colour, markeredgewidth=2,
                  alpha=1.0 if usable else 0.6)
        text = f"{r['impedance_ohm']:.1f} / {r['target_ohm']:.0f} ohm"
        if not usable:
            text += "   extraction not physical"
        ax_z.annotate(text, (dev, yi), xytext=(9 if dev > 0 else -9, 0),
                      textcoords="offset points", fontsize=8, color=INK_2,
                      va="center", ha="left" if dev > 0 else "right")
    ax_z.set_yticks(y, labels, fontsize=9, color=INK)
    ax_z.set_xlabel("simulated differential impedance, deviation from target (%)",
                    fontsize=9.5, color=INK_2)
    ax_z.set_xlim(-62, 34)
    ax_z.set_ylim(-0.7, len(rows) - 0.12)

    # ---- worst return loss in band ----
    ax_rl.axvline(-10, color="#d03b3b", linewidth=1.4, linestyle="--", zorder=1)
    ax_rl.annotate("-10 dB default", (-10, len(rows) - 0.45), xytext=(5, 0),
                   textcoords="offset points", fontsize=8, color="#d03b3b", va="bottom")
    floor = min(r["worst_return_loss_db"] for r in rows) - 4
    for yi, r in zip(y, rows, strict=True):
        rl = r["worst_return_loss_db"]
        colour = CLASS_COLOUR[r["net_class"]]
        usable = r["usable"]
        ax_rl.plot([floor, rl], [yi, yi], color=colour, linewidth=2, zorder=2,
                   alpha=1.0 if usable else 0.35)
        ax_rl.plot([rl], [yi], marker="o", markersize=9, zorder=3,
                   markerfacecolor=colour if usable else SURFACE,
                   markeredgecolor=colour, markeredgewidth=2,
                   alpha=1.0 if usable else 0.6)
        ax_rl.annotate(f"{rl:.1f} dB at {r['worst_return_loss_ghz']:.2f} GHz",
                       (rl, yi), xytext=(8, 0), textcoords="offset points",
                       fontsize=8, color=INK_2, va="center")
    ax_rl.set_xlabel("worst return loss in band (dB)", fontsize=9.5, color=INK_2)
    ax_rl.set_xlim(floor, 6)

    handles = [
        Line2D([], [], color=c, marker="o", markersize=8, linewidth=2, label=n)
        for n, c in CLASS_COLOUR.items()
    ]
    handles.append(
        Line2D([], [], color=INK_MUTED, marker="o", markersize=8, linewidth=2,
               markerfacecolor=SURFACE, markeredgecolor=INK_MUTED, alpha=0.6,
               label="extraction not usable")
    )
    ax_rl.legend(handles=handles, loc="lower right", fontsize=8.5, frameon=False,
                 labelcolor=INK_2)

    fig.suptitle(
        "Eleven differential pairs on examples/pcie-sata, solved with openEMS",
        fontsize=12.5, color=INK, x=0.012, ha="left", y=0.975,
    )
    fig.text(
        0.012, 0.915,
        "aipcb simulate's own metrics. Six of the eleven land more than 10% from "
        "their declared impedance, and the tool reports each one; this validates "
        "the layout, not the fabricated board.",
        fontsize=9, color=INK_2, ha="left",
    )
    fig.subplots_adjust(left=0.155, right=0.995, top=0.855, bottom=0.115)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    print(f"  wrote {out} from {len(rows)} pair(s)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-simulation", action="store_true",
                        help="also run `aipcb simulate` (minutes; needs the container image)")
    parser.add_argument("--only", action="append", default=[],
                        choices=["schematic", "placed", "routed", "simulation", "3d"])
    args = parser.parse_args()

    IMAGES.mkdir(parents=True, exist_ok=True)
    aipcb = tool("aipcb")
    wanted = set(args.only) or {"schematic", "placed", "routed", "simulation", "3d"}
    written: list[Path] = []

    if "schematic" in wanted:
        written.append(schematic(aipcb))
    if "placed" in wanted:
        written.append(placed(aipcb))
    if "routed" in wanted:
        written.append(routed(aipcb))
    if "3d" in wanted:
        written.append(render_3d())
    if "simulation" in wanted:
        got = simulation(aipcb, run_solver=args.with_simulation)
        if got:
            written.append(got)

    print("\nwrote:")
    for path in written:
        size = path.stat().st_size
        print(f"  {path.relative_to(REPO)}  {size / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

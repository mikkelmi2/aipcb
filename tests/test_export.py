# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Fabrication output: the last step of the source-to-fab-data path."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from aipcb.compile.build import build_design
from aipcb.compile.export import export_board, gerber_layers, position_file_name
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import parse

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries


class TestLayerSelection:
    def test_two_layer_board(self) -> None:
        layers = gerber_layers(2)
        assert layers[:2] == ["F.Cu", "B.Cu"]
        assert "Edge.Cuts" in layers

    def test_inner_layers_are_included_in_order(self) -> None:
        assert gerber_layers(4)[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_every_board_gets_mask_paste_and_silk(self) -> None:
        layers = set(gerber_layers(2))
        assert {"F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS"} <= layers


@needs_kicad_libraries
@needs_kicad_cli
class TestExport:
    def _export(self, name: str, tmp_path: Path):
        design = REPO_ROOT / "examples" / name / "design.yaml"
        report = Report()
        build = build_design(design, out_dir=tmp_path / "build", report=report)
        board = next(p for p in build.written if p.suffix == ".kicad_pcb")
        schematic = next(p for p in build.written if p.suffix == ".kicad_sch")
        result = export_board(
            board, tmp_path / "out", build.netlist, report,
            schematic=schematic,
        )
        return result, report

    def test_produces_a_full_fabrication_package(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        result, report = self._export(example_design.parent.name, tmp_path)
        assert result.ok, report.render()
        suffixes = result.by_suffix()
        assert suffixes.get(".gbr", 0) >= 8, suffixes
        assert suffixes.get(".drl", 0) >= 1, "no drill file"
        assert suffixes.get(".gbrjob", 0) == 1, "no job file"

    def test_every_copper_and_technical_layer_is_plotted(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        names = {p.name for p in result.files}
        for layer in ("F_Cu", "B_Cu", "F_Mask", "B_Mask", "F_Silkscreen", "Edge_Cuts"):
            assert any(layer in name for name in names), f"{layer} was not plotted"

    def test_gerbers_are_real_gerbers(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        top = next(p for p in result.files if p.name.endswith("F_Cu.gbr"))
        text = top.read_text(encoding="utf-8")
        assert text.startswith("%TF."), "not an X2 Gerber header"
        assert "M02*" in text, "the file is not terminated"

    def test_drill_file_declares_tools(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        drill = next(p for p in result.files if p.name.endswith("-PTH.drl"))
        text = drill.read_text(encoding="utf-8")
        assert text.startswith("M48"), "not an Excellon header"
        assert "METRIC" in text
        assert any(line.startswith("T1C") for line in text.splitlines()), "no tools"

    def test_bom_groups_identical_parts(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        bom = next(p for p in result.files if p.name == "bom.csv")
        rows = list(csv.DictReader(bom.read_text(encoding="utf-8").splitlines()))
        assert rows
        series = next(r for r in rows if r["Value"] == "R_22R_0603")
        assert series["Reference"] == "R1,R2"
        assert series["QUANTITY"] == "2"

    def test_bom_carries_descriptions_from_the_part_database(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        bom = next(p for p in result.files if p.name == "bom.csv")
        rows = list(csv.DictReader(bom.read_text(encoding="utf-8").splitlines()))
        connector = next(r for r in rows if r["Reference"] == "J1")
        assert "USB 2.0 Micro-B" in connector["Description"]

    def test_placement_file_lists_every_part(self, tmp_path: Path) -> None:
        result, _ = self._export("usb-port", tmp_path)
        positions = next(
            p for p in result.files if p.name == position_file_name(Path("usb-port"))
        )
        rows = list(csv.DictReader(positions.read_text(encoding="utf-8").splitlines()))
        assert len(rows) >= 7

    def test_the_placement_file_is_where_a_consumer_looks_for_it(
        self, tmp_path: Path
    ) -> None:
        """The name has to match the glob, not merely be a name we like.

        A placement file nobody can find is worse than a missing one: the tool that
        wanted it carries on with no ports, no error and no warning. So the test
        expresses the discovery rule as a consumer writes it -- gerber2ems does
        `(cwd / "fab").glob("*pos.csv")`, and KiCad's own file dialog filters on
        `*.pos` / `*pos.csv` -- and asks whether what we emitted is in the result.
        """
        result, _ = self._export("usb-port", tmp_path)
        found = sorted(p.name for p in result.directory.glob("*pos.csv"))
        assert len(found) == 1, f"the *pos.csv glob found {found}"
        assert found[0] in {p.name for p in result.files}


@needs_kicad_libraries
@needs_kicad_cli
class TestExportCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "export", *args],
            capture_output=True, text=True, check=False,
        )

    def test_exports_and_reports_json(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        result = self._run(str(design), "--out", str(tmp_path / "fab"), "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert "gerbers" in payload["export"]["steps"]
        assert "drill" in payload["export"]["steps"]

    def test_leaves_no_build_artefacts_beside_the_source(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        before = set(design.parent.iterdir())
        self._run(str(design), "--out", str(tmp_path / "fab"))
        assert set(design.parent.iterdir()) == before


@needs_kicad_libraries
class TestDrillOrigin:
    """The drill/place file origin, which decides whether fab data is legible.

    ``export`` already asked kicad-cli for coordinates relative to the drill file
    origin (``--use-drill-file-origin``, ``--drill-origin plot``). What was missing
    was the origin itself: with no ``(aux_axis_origin ...)`` in the board, KiCad
    falls back to the page corner, and every drill coordinate comes out negative in
    Y. That is still valid Excellon, so nothing fails -- consumers that parse
    unsigned coordinates simply drop every hole in silence. These tests therefore
    read the exported file rather than trusting the flags, because the flags were
    right the whole time the output was wrong.
    """

    def test_the_board_declares_its_bottom_left_corner_as_the_origin(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        result = build_design(example_design, out_dir=tmp_path / "build")
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        setup = board.child("setup")
        assert setup is not None
        origin = setup.child("aux_axis_origin")
        assert origin is not None, "the board declares no drill/place file origin"

        xs, ys = [], []
        for graphic in ("gr_line", "gr_arc"):
            for node in board.children(graphic):
                if (layer := node.child("layer")) is None or layer.value() != "Edge.Cuts":
                    continue
                for point in ("start", "mid", "end"):
                    if (at := node.child(point)) is not None:
                        xs.append(float(at.value(0) or 0))
                        ys.append(float(at.value(1) or 0))
        assert xs, "the board has no edge"
        # Bottom-left in KiCad's Y-down board space: the smallest X, the largest Y.
        assert (float(origin.value(0) or 0), float(origin.value(1) or 0)) == (
            pytest.approx(min(xs)),
            pytest.approx(max(ys)),
        )

    @needs_kicad_cli
    def test_no_exported_drill_coordinate_is_negative(self, tmp_path: Path) -> None:
        board, netlist, origin = self._routed_board("mcu-4layer", tmp_path)
        vias = [
            (float(at.value(0) or 0), float(at.value(1) or 0))
            for via in board.children("via")
            if (at := via.child("at")) is not None
        ]
        assert vias, "mcu-4layer routed without vias; the test would prove nothing"

        report = Report()
        result = export_board(
            tmp_path / "routed" / "mcu-4layer.kicad_pcb",
            tmp_path / "fab",
            netlist,
            report,
        )
        assert result.ok, report.render()
        # The plated file specifically: the drill export is split by plating, and
        # `mcu-4layer` has no non-plated holes at all, so the NPTH file is empty.
        drill = next(p for p in result.files if p.name.endswith("-PTH.drl"))
        holes = _drill_coordinates(drill)
        assert holes

        negative = [c for c in holes if c[0] < 0 or c[1] < 0]
        assert not negative, f"drill coordinates outside the origin: {negative[:4]}"

        # Every via lands where the declared origin says it should. This is the half
        # a sign check cannot see: an origin nothing was measured from would still
        # pass the test above on a board that happens to sit in the positive
        # quadrant.
        for x, y in vias:
            want = (round(x - origin[0], 3), round(origin[1] - y, 3))
            assert any(
                abs(hx - want[0]) < 1e-3 and abs(hy - want[1]) < 1e-3
                for hx, hy in holes
            ), f"via at {(x, y)} is not in the drill file at {want}"

    @needs_kicad_cli
    def test_the_drill_file_a_consumer_globs_for_exists(self, tmp_path: Path) -> None:
        """``<board>.drl`` is not a name anything downstream looks for.

        With neither ``--excellon-separate-th`` nor a plating split, KiCad emits one
        ``MixedPlating`` file called ``<board>.drl``. It is perfectly good Excellon
        and a human board house reads it happily; the ecosystem's tools do not,
        because they glob for ``-PTH.drl`` (gerber2ems calls ``sys.exit(1)`` when the
        glob is empty) and because a fab that plates by file expects the split. So
        the test asks the question the consumer asks -- is there exactly one PTH
        file -- rather than "did we pass the flag".
        """
        result, _ = self._export_only("mcu-4layer", tmp_path)
        pth = sorted(p.name for p in result.directory.glob("*-PTH.drl"))
        assert len(pth) == 1, f"the *-PTH.drl glob found {pth}"
        npth = sorted(p.name for p in result.directory.glob("*-NPTH.drl"))
        assert len(npth) == 1, f"the *-NPTH.drl glob found {npth}"
        assert not list(result.directory.glob("mcu-4layer.drl")), (
            "the unsplit MixedPlating drill file is still being written"
        )
        assert _drill_coordinates(result.directory / pth[0]), "the PTH file has no holes"

    @needs_kicad_cli
    def test_placement_coordinates_are_measured_from_the_drill_origin(
        self, tmp_path: Path
    ) -> None:
        """The placement file's *frame*, which is the half a filename fix cannot see.

        Naming the file ``*-all-pos.csv`` made it discoverable. It did not make it
        right: ``pcb export pos`` defaults to absolute page coordinates, so every row
        read ``145.5, -122.5`` -- the board's position on an A4 sheet, with KiCad's
        downward Y. Consumers place things relative to the board's own corner, so a
        port read out of that file would land somewhere off the board entirely, and
        nothing would say so.

        Read the file, not the flag: every row must sit inside the board's bounding
        box, and each one must equal the footprint's own offset from the declared
        origin.
        """
        board, netlist, origin = self._routed_board("mcu-4layer", tmp_path)
        placed = {
            str(fp.child("property").value(1)): (
                float(at.value(0) or 0), float(at.value(1) or 0)
            )
            for fp in board.children("footprint")
            if (at := fp.child("at")) is not None
            and fp.child("property") is not None
        }
        assert placed, "the board has no footprints; the test would prove nothing"

        report = Report()
        result = export_board(
            tmp_path / "routed" / "mcu-4layer.kicad_pcb",
            tmp_path / "fab",
            netlist,
            report,
        )
        assert result.ok, report.render()
        positions = next(p for p in result.files if p.name.endswith("pos.csv"))
        rows = list(csv.DictReader(positions.read_text(encoding="utf-8").splitlines()))
        assert rows

        negative = [r for r in rows if float(r["PosX"]) < 0 or float(r["PosY"]) < 0]
        assert not negative, (
            "placement coordinates outside the board's own corner: "
            f"{[(r['Ref'], r['PosX'], r['PosY']) for r in negative[:4]]}"
        )
        for row in rows:
            want = placed.get(row["Ref"])
            assert want is not None, f"{row['Ref']} is not on the board"
            assert (float(row["PosX"]), float(row["PosY"])) == (
                pytest.approx(want[0] - origin[0], abs=1e-3),
                pytest.approx(origin[1] - want[1], abs=1e-3),
            ), f"{row['Ref']} is not placed relative to the drill/place file origin"

    def _export_only(self, name: str, tmp_path: Path):
        design = REPO_ROOT / "examples" / name / "design.yaml"
        report = Report()
        build = build_design(design, out_dir=tmp_path / "build", report=report)
        board = next(p for p in build.written if p.suffix == ".kicad_pcb")
        result = export_board(board, tmp_path / "fab", build.netlist, report)
        assert result.ok, report.render()
        return result, report

    def _routed_board(self, name: str, tmp_path: Path):
        design = REPO_ROOT / "examples" / name / "design.yaml"
        run = subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", "all",
             str(design), "--out", str(tmp_path / "routed")],
            capture_output=True, text=True, check=False,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        netlist = build_design(design, out_dir=tmp_path / "build").netlist
        board = parse(
            (tmp_path / "routed" / f"{name}.kicad_pcb").read_text(encoding="utf-8")
        )
        setup = board.child("setup")
        assert setup is not None
        origin = setup.child("aux_axis_origin")
        assert origin is not None
        return board, netlist, (float(origin.value(0) or 0), float(origin.value(1) or 0))


def _drill_coordinates(path: Path) -> list[tuple[float, float]]:
    """Every hole position in an Excellon file, as the fab's reader sees them."""
    out: list[tuple[float, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        for match in re.finditer(r"X(-?[0-9.]+)Y(-?[0-9.]+)", line):
            out.append((float(match.group(1)), float(match.group(2))))
    return out

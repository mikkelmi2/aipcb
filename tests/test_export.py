"""Fabrication output: the last step of the source-to-fab-data path."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from aipcb.compile.build import build_design
from aipcb.compile.export import export_board, gerber_layers
from aipcb.diagnostics import Report

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
        drill = next(p for p in result.files if p.suffix == ".drl")
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
        positions = next(p for p in result.files if p.name == "positions.csv")
        rows = list(csv.DictReader(positions.read_text(encoding="utf-8").splitlines()))
        assert len(rows) >= 7


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

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The assembly outputs: a BOM and a centroid file an assembler accepts (M21).

Two kinds of test here. The row builders are exercised directly against synthetic
parts, because that is where every fab-specific rule lives and each one deserves to
fail on its own. The end-to-end tests run the real exporter over a bundled example,
because the one claim a unit test cannot make is that the centroid agrees with the
Gerbers -- and that claim rests on both coming from the same `kicad-cli` run.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import ClassVar

import pytest

from aipcb.compile.assembly import (
    FORMATS,
    PlacedPart,
    bom_rows,
    cpl_rows,
    export_assembly,
    read_positions,
)
from aipcb.diagnostics import Report, Severity
from aipcb.kicad.sexpr import parse
from aipcb.model.parts import Assembly, Part

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

PCIE = REPO_ROOT / "examples" / "pcie-sata" / "design.yaml"


def placed(
    refdes: str,
    *,
    value: str = "100n",
    rotation: float = 0.0,
    side: str = "top",
    assembly: Assembly = Assembly.SMT,
    mpn: str | None = None,
    refs: dict[str, str] | None = None,
    x: float = 1.0,
    y: float = 2.0,
) -> PlacedPart:
    return PlacedPart(
        refdes=refdes,
        value=value,
        package="C_0603_1608Metric",
        footprint="Capacitor_SMD:C_0603_1608Metric",
        x=x,
        y=y,
        rotation=rotation,
        side=side,
        assembly=assembly,
        mpn=mpn,
        manufacturer="ACME" if mpn else None,
        supplier_refs=refs or {},
        description="a capacitor",
    )


class TestTheCentroidFile:
    def test_jlcpcb_uses_jlcpcbs_own_column_names(self) -> None:
        rows = cpl_rows([placed("C1")], FORMATS["jlcpcb"])
        assert list(rows[0]) == ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"]

    def test_the_side_is_spelled_the_way_each_fab_spells_it(self) -> None:
        part = placed("C1")
        assert cpl_rows([part], FORMATS["jlcpcb"])[0]["Layer"] == "Top"
        assert cpl_rows([part], FORMATS["generic"])[0]["Side"] == "top"

    @pytest.mark.parametrize(
        ("kicad", "expected"),
        [(0.0, "0.0000"), (90.0, "90.0000"), (-90.0, "270.0000"),
         (180.0, "180.0000"), (-180.0, "180.0000"), (360.0, "0.0000"),
         (-0.0, "0.0000"), (450.0, "90.0000")],
    )
    def test_rotation_is_folded_into_the_range_every_fab_samples(
        self, kicad: float, expected: str
    ) -> None:
        """KiCad writes signed degrees; the fabs' samples are 0..360. Same angle.

        This is a fold and not a correction -- both fabs document the convention
        KiCad already uses, degrees counter-clockwise positive, so no part turns.
        """
        rows = cpl_rows([placed("C1", rotation=kicad)], FORMATS["jlcpcb"])
        assert rows[0]["Rotation"] == expected

    def test_pcbway_lists_only_surface_mount_parts(self) -> None:
        """"Only surface mounting parts are listed in the Centroid." -- PCBWay."""
        parts = [placed("C1"), placed("J1", assembly=Assembly.THT)]
        assert [r["Designator"] for r in cpl_rows(parts, FORMATS["pcbway"])] == ["C1"]

    def test_jlcpcb_keeps_through_hole_parts_because_it_does_not_say_otherwise(
        self,
    ) -> None:
        parts = [placed("C1"), placed("J1", assembly=Assembly.THT)]
        assert [r["Designator"] for r in cpl_rows(parts, FORMATS["jlcpcb"])] == [
            "C1",
            "J1",
        ]

    def test_a_do_not_populate_part_is_never_placed(self) -> None:
        parts = [placed("C1"), placed("C2", assembly=Assembly.DNP)]
        for name in FORMATS:
            designators = [r["Designator"] for r in cpl_rows(parts, FORMATS[name])]
            assert "C2" not in designators, name

    def test_a_footprint_that_is_not_a_part_is_never_placed(self) -> None:
        """A card-edge finger field is geometry, not something to fit."""
        parts = [placed("C1"), placed("J1", assembly=Assembly.NONE)]
        assert [r["Designator"] for r in cpl_rows(parts, FORMATS["jlcpcb"])] == ["C1"]


class TestTheBillOfMaterials:
    def test_jlcpcb_uses_jlcpcbs_own_column_names(self) -> None:
        rows = bom_rows([placed("C1")], FORMATS["jlcpcb"])
        assert list(rows[0]) == ["Comment", "Designator", "Footprint", "JLCPCB Part #"]

    def test_identical_parts_become_one_line_with_their_designators_aggregated(
        self,
    ) -> None:
        parts = [placed("C1"), placed("C2"), placed("C10")]
        rows = bom_rows(parts, FORMATS["jlcpcb"])
        assert len(rows) == 1
        assert rows[0]["Designator"] == "C1,C2,C10"

    def test_designators_sort_the_way_a_human_reads_them(self) -> None:
        """C10 after C2, which no plain string sort does."""
        rows = bom_rows([placed("C10"), placed("C2"), placed("C1")], FORMATS["jlcpcb"])
        assert rows[0]["Designator"] == "C1,C2,C10"

    def test_parts_with_different_part_numbers_are_different_lines(self) -> None:
        parts = [placed("C1", mpn="ONE"), placed("C2", mpn="TWO")]
        assert len(bom_rows(parts, FORMATS["jlcpcb"])) == 2

    def test_the_same_part_number_is_one_line_whatever_the_footprint(self) -> None:
        """A buyer orders a part number, so that is what groups a purchase line."""
        parts = [placed("C1", mpn="SAME"), placed("C2", mpn="SAME")]
        rows = bom_rows(parts, FORMATS["generic"])
        assert len(rows) == 1 and rows[0]["Quantity"] == "2"

    def test_the_fabs_own_part_number_column_is_filled_from_supplier_refs(self) -> None:
        part = placed("C1", mpn="X", refs={"lcsc": "C1525", "digikey": "311-1"})
        assert bom_rows([part], FORMATS["jlcpcb"])[0]["JLCPCB Part #"] == "C1525"

    def test_a_do_not_populate_part_says_so_where_a_human_reads_it(self) -> None:
        """Neither fab documents a machine-readable DNP, so it goes in the text."""
        rows = bom_rows([placed("C1", assembly=Assembly.DNP)], FORMATS["jlcpcb"])
        assert "DNP" in rows[0]["Comment"]

    def test_a_footprint_that_is_not_a_part_is_not_on_the_bill(self) -> None:
        parts = [placed("C1"), placed("J1", assembly=Assembly.NONE)]
        rows = bom_rows(parts, FORMATS["generic"])
        assert [r["Designators"] for r in rows] == ["C1"]

    def test_pcbway_numbers_its_lines_from_one(self) -> None:
        rows = bom_rows([placed("C1"), placed("R1", value="10k")], FORMATS["pcbway"])
        assert [r["Line#"] for r in rows] == ["1", "2"]

    def test_pcbway_is_told_which_parts_are_through_hole(self) -> None:
        parts = [placed("C1"), placed("J1", assembly=Assembly.THT)]
        types = {
            r["Reference Designator"]: r["Type"] for r in bom_rows(parts, FORMATS["pcbway"])
        }
        assert types == {"C1": "Surface mount", "J1": "Thru-hole"}

    def test_parts_needing_different_handling_never_share_a_line(self) -> None:
        """One line cannot be both fitted and not, or both SMT and through-hole.

        Caught by the test above when the grouping key was the part number alone:
        a do-not-populate instance merged with a fitted one and the merged line
        was marked whichever the first member happened to be.
        """
        parts = [
            placed("C1", mpn="SAME"),
            placed("C2", mpn="SAME", assembly=Assembly.DNP),
            placed("C3", mpn="SAME", assembly=Assembly.THT),
        ]
        rows = bom_rows(parts, FORMATS["generic"])
        assert {r["Designators"]: r["Assembly"] for r in rows} == {
            "C1": "smt", "C2": "dnp", "C3": "tht",
        }


class TestTheSchemaStillTakesTheOldSpelling:
    def test_supplier_mpn_still_reaches_the_bill_of_materials(self) -> None:
        """`supplier: {mpn: ...}` predates M21a and two examples still use it."""
        part = Part(
            symbol="Device:C",
            footprint="Capacitor_SMD:C_0603_1608Metric",
            pins={"1": {}},
            supplier={"mpn": "OLD-1", "manufacturer": "Maker"},
        )
        assert part.procurement() == ("OLD-1", "Maker")

    def test_the_new_spelling_wins_and_a_disagreement_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="declared twice"):
            Part(
                symbol="Device:C",
                footprint="Capacitor_SMD:C_0603_1608Metric",
                pins={"1": {}},
                mpn="NEW",
                supplier={"mpn": "OLD"},
            )

    def test_a_part_that_says_nothing_still_validates(self) -> None:
        part = Part(
            symbol="Device:C", footprint="Capacitor_SMD:C_0603_1608Metric", pins={"1": {}}
        )
        assert part.procurement() == (None, None) and part.assembly is None


@needs_kicad_libraries
@needs_kicad_cli
class TestAgainstARealBoard:
    """The claims a unit test cannot make, on `examples/pcie-sata`."""

    @staticmethod
    def _export(tmp_path: Path, **kwargs: object) -> tuple[Path, Report]:
        from aipcb.compile.build import build_design
        from aipcb.compile.export import export_board

        report = Report()
        build = tmp_path / "build"
        out = tmp_path / "out"
        result = build_design(PCIE, out_dir=build, report=report)
        board = next(p for p in result.written if p.suffix == ".kicad_pcb")
        sch = next(p for p in result.written if p.suffix == ".kicad_sch")
        export_board(
            board, out, result.netlist, report, schematic=sch,
            assembly_formats=("jlcpcb", "pcbway", "generic"), **kwargs,  # type: ignore[arg-type]
        )
        return out / "assembly", report

    def test_the_centroid_agrees_with_the_placement_file_kicad_wrote(
        self, tmp_path: Path
    ) -> None:
        """The claim the whole module rests on: one measurement, two views.

        The Gerbers and the placement file are both plotted with
        `--use-drill-file-origin`, and the centroid is derived from the placement
        file rather than from the board a second time. So every coordinate in the
        assembler's file is a coordinate the fabricator's files agree with, by
        construction rather than by two derivations happening to match.
        """
        assembly, _ = self._export(tmp_path)
        positions = {
            row[0]: (row[3], row[4])
            for row in read_positions(
                tmp_path / "out" / "pcie-sata-all-pos.csv"
            )
        }
        with (assembly / "pcie-sata-cpl-jlcpcb.csv").open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                x, y = positions[row["Designator"]]
                assert float(row["Mid X"]) == pytest.approx(x)
                assert float(row["Mid Y"]) == pytest.approx(y)

    def test_every_designator_on_the_centroid_is_on_the_bill(self, tmp_path: Path) -> None:
        """JLCPCB matches the two files by designator and rejects a mismatch."""
        assembly, _ = self._export(tmp_path)

        def column(name: str, field: str) -> set[str]:
            with (assembly / name).open(encoding="utf-8") as handle:
                found: set[str] = set()
                for row in csv.DictReader(handle):
                    found.update(d for d in row[field].split(",") if d)
                return found

        on_bom = column("pcie-sata-bom-jlcpcb.csv", "Designator")
        on_cpl = column("pcie-sata-cpl-jlcpcb.csv", "Designator")
        assert on_cpl <= on_bom, sorted(on_cpl - on_bom)

    def test_the_package_is_byte_stable(self, tmp_path: Path) -> None:
        first, _ = self._export(tmp_path / "a")
        second, _ = self._export(tmp_path / "b")
        names = sorted(p.name for p in first.iterdir())
        assert names == sorted(p.name for p in second.iterdir())
        for name in names:
            assert (first / name).read_bytes() == (second / name).read_bytes(), name

    def test_the_missing_part_numbers_are_named_one_by_one(self, tmp_path: Path) -> None:
        """The warning is the deliverable: an order is blocked by *these* parts.

        `J2`-`J5` are the JST connectors, which carry a real part number, so they
        are absent from the list -- and `J1` is the card-edge finger field, which
        is `assembly: none` and is not a part at all. What is left is the six
        capacitors and the controller, which is the state M21's report records.
        """
        _, report = self._export(tmp_path)
        missing = [d for d in report if d.code == "assembly-missing-mpn"]
        assert len(missing) == 1
        assert missing[0].severity is Severity.WARNING
        named = missing[0].message.split(":")[-1].strip()
        assert named == "C1, C2, C3, C4, C5, C6, U1"

    def test_the_overlay_is_drawn_from_the_centroid_file(self, tmp_path: Path) -> None:
        assembly, _ = self._export(tmp_path)
        svg = (assembly / "pcie-sata-placement-top.svg").read_text(encoding="utf-8")
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
        with (assembly / "pcie-sata-cpl-jlcpcb.csv").open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                assert f">{row['Designator']}<" in svg

    def test_it_matches_the_committed_package(self, tmp_path: Path) -> None:
        """The acceptance artifact (M21d), reviewed once and then guarded.

        `tests/golden/pcie-sata/assembly/` is the package a person read line by
        line before it was committed -- the checklist is in `docs/reports/m21.md`
        §5. What this test protects is that nothing changes it by accident: a
        column renamed, a part appearing or vanishing, a rotation moving. If the
        change is intended, run `python -m tests.regenerate_golden` and read the
        diff, which is exactly the diff an assembler would have received.
        """
        golden = REPO_ROOT / "tests" / "golden" / "pcie-sata" / "assembly"
        assembly, _ = self._export(tmp_path)
        for expected in sorted(p for p in golden.iterdir() if p.is_file()):
            actual = assembly / expected.name
            assert actual.exists(), f"{expected.name} was not produced"
            assert actual.read_text(encoding="utf-8") == expected.read_text(
                encoding="utf-8"
            ), (
                f"{expected.name} differs from the committed package. If the change "
                "is intended, run `python -m tests.regenerate_golden` and review it."
            )

    def test_the_bundle_holds_everything_that_is_sent(self, tmp_path: Path) -> None:
        import zipfile

        assembly, _ = self._export(tmp_path, bundle=True)
        archive = assembly / "pcie-sata-assembly.zip"
        with zipfile.ZipFile(archive) as zipped:
            held = set(zipped.namelist())
        expected = {p.name for p in assembly.iterdir() if p != archive}
        assert held == expected


@needs_kicad_libraries
@needs_kicad_cli
class TestTheBackSideGuard:
    def test_a_back_side_part_refuses_rather_than_placing_it_on_the_front(
        self, tmp_path: Path
    ) -> None:
        """`side: back` validates, warns, and places on the front (M9).

        An assembly package that quietly described that as a back-side placement
        would be a file that does not describe the board -- so it is refused, and
        the refusal names the parts.
        """
        from aipcb.compile.assembly import placed_parts

        board = parse(
            (REPO_ROOT / "tests" / "golden" / "ldo-supply" / "ldo-supply.kicad_pcb")
            .read_text(encoding="utf-8")
        )
        report = Report()
        positions = [("C1", "100n", "C_0603", 1.0, 2.0, 0.0, "bottom")]

        class _Netlist:
            name = "ldo-supply"
            components: ClassVar[dict[str, object]] = {}

        parts = placed_parts(board, _Netlist(), positions)  # type: ignore[arg-type]
        assert parts[0].side == "bottom"

        pos_csv = tmp_path / "pos.csv"
        pos_csv.write_text(
            "Ref,Val,Package,PosX,PosY,Rot,Side\n"
            '"C1","100n","C_0603",1.0,2.0,0.0,bottom\n',
            encoding="utf-8",
        )
        outcome = export_assembly(
            tmp_path / "ldo-supply.kicad_pcb",
            board,
            pos_csv,
            tmp_path / "assembly",
            _Netlist(),  # type: ignore[arg-type]
            report,
            formats=("generic",),
        )
        assert not outcome.ok
        codes = [d.code for d in report if d.severity is Severity.ERROR]
        assert "assembly-back-side-unsupported" in codes

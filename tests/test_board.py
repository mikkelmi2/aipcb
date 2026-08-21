"""Compiling designs into KiCad boards.

The acceptance bar for M3 is a board KiCad loads, whose DRC reports no violations
and whose schematic parity is clean -- meaning every footprint is tied to its
symbol and every pad sits on the net the source says it does. Routing does not
exist yet, so unconnected ratlines are expected and are not failures.
"""

from __future__ import annotations

import json
from pathlib import Path

from aipcb.compile.board import standard_layers, unconnected_net_name
from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.place import plan_placement, usable_area
from aipcb.diagnostics import Report
from aipcb.kicad.cli import run_kicad
from aipcb.kicad.footprints import footprint_extent, resolve_footprint
from aipcb.kicad.sexpr import Atom, parse
from aipcb.model.board import Outline
from aipcb.model.layout import BoardOutline

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries


def board_of(design: Path, tmp_path: Path) -> Path:
    build_design(design, out_dir=tmp_path)
    return next(tmp_path.glob("*.kicad_pcb"))


# ---------------------------------------------------------------------------
# layers and stackup
# ---------------------------------------------------------------------------


class TestLayers:
    def test_two_layer_board(self) -> None:
        layers = standard_layers(2)
        names = [entry.value(0) for entry in layers.children()]
        assert names[:2] == ["F.Cu", "B.Cu"]
        assert "Edge.Cuts" in names

    def test_four_layer_board_numbers_inner_layers(self) -> None:
        layers = standard_layers(4)
        names = [entry.value(0) for entry in layers.children()]
        assert names[:4] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def test_back_copper_keeps_its_reserved_number(self) -> None:
        """KiCad reserves 0 for F.Cu and 2 for B.Cu whatever the layer count."""
        for count in (2, 4, 6):
            entries = {e.value(0): e.name for e in standard_layers(count).children()}
            assert entries["F.Cu"] == "0"
            assert entries["B.Cu"] == "2"


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestPlacement:
    def _placement(self, name: str, report: Report | None = None):
        netlist = compile_netlist(REPO_ROOT / "examples" / name / "design.yaml", Report())
        extents = {
            c.refdes: footprint_extent(resolve_footprint(c.part.footprint))
            for c in netlist.sorted_components()
            if c.part is not None
        }
        return netlist, plan_placement(netlist, report=report, extents=extents)

    def test_every_component_is_placed(self, example_design: Path) -> None:
        netlist, placement = self._placement(example_design.parent.name)
        assert set(placement.positions) == set(netlist.components)

    def test_placement_is_deterministic(self) -> None:
        _, first = self._placement("usb-port")
        _, second = self._placement("usb-port")
        assert first.positions == second.positions

    def test_no_two_components_share_a_position(self, example_design: Path) -> None:
        _, placement = self._placement(example_design.parent.name)
        seen = [(p.x, p.y) for p in placement.positions.values()]
        assert len(set(seen)) == len(seen)

    def test_grouped_components_end_up_together(self) -> None:
        """The `group` constraint on the USB series resistors must actually hold."""
        _, placement = self._placement("usb-port")
        r1, r2 = placement["R1"], placement["R2"]
        distance = ((r1.x - r2.x) ** 2 + (r1.y - r2.y) ** 2) ** 0.5
        assert distance < 15.0, f"R1 and R2 ended up {distance:.1f} mm apart"

    def test_decoupling_follows_its_target(self) -> None:
        """`for:` is a placement relationship, not just documentation."""
        _, placement = self._placement("led-blinker")
        c1, u1 = placement["C1"], placement["U1"]
        distance = ((c1.x - u1.x) ** 2 + (c1.y - u1.y) ** 2) ** 0.5
        assert distance < 20.0, f"C1 ended up {distance:.1f} mm from U1"

    def test_examples_fit_their_outlines(self, example_design: Path) -> None:
        report = Report()
        self._placement(example_design.parent.name, report)
        overflow = [d for d in report if d.code == "placement-overflow"]
        assert not overflow, overflow[0].message if overflow else ""

    def test_overflow_reports_the_size_that_would_fit(self) -> None:
        report = Report()
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "usb-port" / "design.yaml", Report()
        )
        assert netlist.board is not None
        netlist.board = netlist.board.model_copy(
            update={"outline": Outline(rect=(10.0, 10.0))}
        )
        plan_placement(netlist, report=report, extents={})
        diag = next(d for d in report if d.code == "placement-overflow")
        assert "they need about" in diag.message

    def test_usable_area_subtracts_the_margin_twice(self) -> None:
        outline = BoardOutline(shape="rect", width_mm=50.0, height_mm=30.0)
        assert usable_area(outline, 2.0) == (46.0, 26.0)


# ---------------------------------------------------------------------------
# board structure
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestBoardStructure:
    def test_board_parses(self, example_design: Path, tmp_path: Path) -> None:
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        assert root.name == "kicad_pcb"
        assert root.get("version") == "20241229"

    def test_net_zero_comes_first(self, example_design: Path, tmp_path: Path) -> None:
        """KiCad reserves net 0 for 'no net' and expects it at the top of the table."""
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        nets = list(root.children("net"))
        assert nets[0].value(0) == "0"
        assert nets[0].value(1) == ""

    def test_every_component_becomes_a_footprint(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        netlist = compile_netlist(example_design, Report())
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        placed = {
            next(
                p.value(1) for p in fp.children("property") if p.value(0) == "Reference"
            )
            for fp in root.children("footprint")
        }
        assert placed == set(netlist.components)

    def test_footprints_link_back_to_their_symbols(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """The `path` token is what makes schematic parity work."""
        netlist = compile_netlist(example_design, Report())
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        paths = {fp.get("path") for fp in root.children("footprint")}
        for component in netlist.components.values():
            assert f"/{component.uuid}" in paths

    def test_footprint_name_stays_the_first_token(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """A footprint whose library id is not first fails to load, silently."""
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        for fp in root.children("footprint"):
            assert isinstance(fp.items[0], Atom), (
                f"the first item is {type(fp.items[0]).__name__}, not the library id"
            )
            assert ":" in fp.items[0].value

    def test_only_drawables_carry_uuids(self, example_design: Path, tmp_path: Path) -> None:
        """A uuid on a structural token makes KiCad refuse the file outright."""
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        for fp in root.children("footprint"):
            for child in fp.children():
                if child.name in ("descr", "tags", "attr", "layer", "at", "sheetname"):
                    assert child.child("uuid") is None, f"{child.name} carries a uuid"

    def test_board_outline_is_closed(self, example_design: Path, tmp_path: Path) -> None:
        """Every edge's end is another edge's start -- arcs included.

        A shaped board's edge is drawn with `gr_arc` as well as `gr_line`, and an
        arc that does not join the segments either side of it is a gap the
        fabricator mills straight through.
        """
        root = parse(board_of(example_design, tmp_path).read_text(encoding="utf-8"))
        edges = [
            g
            for g in root.children()
            if g.name in ("gr_line", "gr_arc") and g.get("layer") == "Edge.Cuts"
        ]
        assert len(edges) >= 3
        starts = {(g.child("start").value(0), g.child("start").value(1)) for g in edges}
        ends = {(g.child("end").value(0), g.child("end").value(1)) for g in edges}
        assert starts == ends, "the outline does not form a closed loop"

    def test_pads_carry_their_nets(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        netlist = compile_netlist(design, Report())
        root = parse(board_of(design, tmp_path).read_text(encoding="utf-8"))
        by_ref = {
            next(p.value(1) for p in fp.children("property") if p.value(0) == "Reference"): fp
            for fp in root.children("footprint")
        }
        component = netlist.components["R1"]
        for pad in by_ref["R1"].children("pad"):
            net = pad.child("net")
            assert net is not None
            expected = component.connections.get(str(pad.value(0)))
            if expected is not None:
                assert net.value(1) == expected

    def test_net_classes_reach_the_project_file(self, tmp_path: Path) -> None:
        """Layer 2's net classes have to become real KiCad design rules."""
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        build_design(design, out_dir=tmp_path)
        project = json.loads(
            next(tmp_path.glob("*.kicad_pro")).read_text(encoding="utf-8")
        )
        classes = {c["name"]: c for c in project["net_settings"]["classes"]}
        assert "usb" in classes
        assert classes["usb"]["diff_pair_width"] == 0.34
        assert classes["usb"]["diff_pair_gap"] == 0.2
        patterns = {
            p["pattern"]: p["netclass"]
            for p in project["net_settings"]["netclass_patterns"]
        }
        assert patterns["USB_DP"] == "usb"

    def test_stackup_thickness_matches_the_source(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        root = parse(board_of(design, tmp_path).read_text(encoding="utf-8"))
        general = root.child("general")
        assert general is not None
        assert general.get("thickness") == "1.6"


class TestUnconnectedNetNames:
    def test_matches_kicads_convention(self) -> None:
        assert (
            unconnected_net_name("U1", "XTAL1/PB3", "2")
            == "unconnected-(U1-XTAL1{slash}PB3-Pad2)"
        )

    def test_plain_names_are_untouched(self) -> None:
        assert unconnected_net_name("R5", "A", "1") == "unconnected-(R5-A-Pad1)"


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestDeterminism:
    def test_two_builds_are_byte_identical(self, example_design: Path, tmp_path: Path) -> None:
        first = board_of(example_design, tmp_path / "a").read_bytes()
        second = board_of(example_design, tmp_path / "b").read_bytes()
        assert first == second


# ---------------------------------------------------------------------------
# what KiCad thinks
# ---------------------------------------------------------------------------


@needs_kicad_libraries
@needs_kicad_cli
class TestAgainstKicad:
    def _drc(self, design: Path, tmp_path: Path) -> dict:
        board = board_of(design, tmp_path)
        report_path = tmp_path / "drc.json"
        run = run_kicad(
            "pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
            "-o", str(report_path), str(board),
        )
        assert run.returncode == 0, f"KiCad could not load the board: {run.stdout}{run.stderr}"
        return json.loads(report_path.read_text(encoding="utf-8"))

    def test_board_loads_and_drc_is_clean(self, example_design: Path, tmp_path: Path) -> None:
        payload = self._drc(example_design, tmp_path)
        violations = [
            f"[{v['severity']}] {v['type']}: {v['description']}"
            for v in payload["violations"]
        ]
        assert not violations, "\n".join(violations)

    def test_schematic_parity_is_clean(self, example_design: Path, tmp_path: Path) -> None:
        """Every footprint tied to its symbol, every pad on the net the source says."""
        payload = self._drc(example_design, tmp_path)
        issues = [
            f"[{v['severity']}] {v['type']}: {v['description']}"
            for v in payload["schematic_parity"]
        ]
        assert not issues, "\n".join(issues)

    def test_only_unrouted_nets_remain(self, example_design: Path, tmp_path: Path) -> None:
        """Ratlines are expected before routing; anything else is not."""
        payload = self._drc(example_design, tmp_path)
        kinds = {item["type"] for item in payload["unconnected_items"]}
        assert kinds <= {"unconnected_items"}, kinds

    def test_board_renders(self, example_design: Path, tmp_path: Path) -> None:
        board = board_of(example_design, tmp_path)
        out = tmp_path / "plot.pdf"
        run = run_kicad(
            "pcb", "export", "pdf", "--layers", "F.Cu,F.SilkS,Edge.Cuts",
            "-o", str(out), str(board),
        )
        assert run.returncode == 0, run.stderr
        assert out.exists() and out.stat().st_size > 1000

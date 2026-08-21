"""Copper pours, stitching vias and plane integrity (M10).

The tests here split three ways, and the split is deliberate:

* **Schema and emission** need nothing but the model. They assert what the source
  can say and what the zone written from it contains -- in particular that it
  contains no filled polygon, which is the whole of the stability policy.
* **Analysis** runs on hand-built boards, because a synthetic board with one track
  slicing one pour is the only way to assert an *exact* island count.
* **The filled board** needs KiCad, and is where the claims that matter are
  checked by reading copper back rather than by trusting a parameter: that a
  thermal pad really has spokes, that a solid one really does not, and that a
  board with a filled pour really passes DRC.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aipcb.checks.mapping import build_index
from aipcb.checks.planes import analyse_planes
from aipcb.checks.pours import run_pour_checks
from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.frame import frame_for
from aipcb.compile.zones import apply_pad_connect, pad_zone_connect, zone_nodes, zone_uuid
from aipcb.diagnostics import Report, Severity
from aipcb.elaborate import elaborate
from aipcb.ids import net_codes
from aipcb.kicad.fill import (
    PCBNEW_PYTHON_ENV,
    FillError,
    fill_board,
    find_pcbnew_python,
    same_version,
    version_number,
)
from aipcb.kicad.sexpr import SNode, dump, num, parse, quoted, sym
from aipcb.loader import load_design
from aipcb.model.design import Design
from aipcb.model.pours import Pour, PourRegion, Stitching
from aipcb.route.emit import attach_copper
from aipcb.route.plan import route_board
from aipcb.route.stitch import MAX_STITCH_VIAS, stitch_board, stitch_uuid

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

USB_PORT = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
QFN = REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
MCU4 = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"


def needs_pcbnew() -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        find_pcbnew_python() is None,
        reason="no interpreter on this machine can import pcbnew; install KiCad's "
        f"Python module or set {PCBNEW_PYTHON_ENV}",
    )


HAS_PCBNEW = needs_pcbnew()


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------


class TestPourSchema:
    def test_one_layer_or_a_list_but_not_both(self) -> None:
        with pytest.raises(ValidationError, match="not both and not neither"):
            Pour.model_validate({"net": "GND", "layer": "F.Cu", "layers": ["B.Cu"]})
        with pytest.raises(ValidationError, match="not both and not neither"):
            Pour.model_validate({"net": "GND"})

    def test_layers_normalise_to_one_tuple(self) -> None:
        assert Pour.model_validate({"net": "GND", "layer": "F.Cu"}).copper_layers == ("F.Cu",)
        assert Pour.model_validate(
            {"net": "GND", "layers": ["F.Cu", "B.Cu"]}
        ).copper_layers == ("F.Cu", "B.Cu")

    def test_scope_and_region_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="scoped either"):
            Pour.model_validate(
                {
                    "net": "GND",
                    "layer": "F.Cu",
                    "scope": "board",
                    "region": {"rect": [[0, 0], [10, 10]]},
                }
            )

    def test_neither_means_the_whole_board(self) -> None:
        pour = Pour.model_validate({"net": "GND", "layer": "F.Cu"})
        assert pour.ring() is None

    def test_below_area_needs_an_area(self) -> None:
        with pytest.raises(ValidationError, match="island_area_min"):
            Pour.model_validate(
                {"net": "GND", "layer": "F.Cu", "remove_islands": "below_area"}
            )
        with pytest.raises(ValidationError, match="only means anything"):
            Pour.model_validate(
                {"net": "GND", "layer": "F.Cu", "island_area_min": 2.0}
            )

    def test_a_region_needs_one_shape_with_area(self) -> None:
        with pytest.raises(ValidationError, match="not both and not neither"):
            PourRegion.model_validate({})
        with pytest.raises(ValidationError, match="no area"):
            PourRegion.model_validate({"rect": [[0, 0], [0, 10]]})

    def test_a_layer_must_be_a_copper_layer(self) -> None:
        with pytest.raises(ValidationError):
            Pour.model_validate({"net": "GND", "layer": "F.SilkS"})

    def test_pad_references_carry_an_instance_suffix(self) -> None:
        pour = Pour.model_validate(
            {
                "net": "GND",
                "layer": "F.Cu",
                "pad_connect": [{"pads": ["U2.4", "U2.4#2", "J1.6#12"], "connect": "solid"}],
            }
        )
        assert pour.pad_connect[0].pads == ("U2.4", "U2.4#2", "J1.6#12")
        with pytest.raises(ValidationError):
            Pour.model_validate(
                {
                    "net": "GND",
                    "layer": "F.Cu",
                    "pad_connect": [{"pads": ["not a pad"], "connect": "solid"}],
                }
            )


class TestStitchingSchema:
    def test_a_ring_needs_something_to_circle(self) -> None:
        with pytest.raises(ValidationError, match="circles something"):
            Stitching.model_validate({"net": "GND", "pattern": "ring", "pitch": 2.0})

    def test_around_only_means_anything_for_a_ring(self) -> None:
        with pytest.raises(ValidationError, match="has no use for one"):
            Stitching.model_validate(
                {"net": "GND", "pattern": "grid", "pitch": 2.0, "around": "U1"}
            )

    def test_a_via_joins_two_different_layers(self) -> None:
        with pytest.raises(ValidationError, match="to itself"):
            Stitching.model_validate(
                {"net": "GND", "pitch": 2.0, "between": ["F.Cu", "F.Cu"]}
            )


class TestBackwardCompatibility:
    def test_a_design_without_pours_is_unchanged(self) -> None:
        """The whole backward-compatibility bar, in one assertion."""
        design = Design.model_validate(
            {"name": "bare", "components": {"R1": {"part": "R_10K_0603"}}}
        )
        assert design.pours == ()
        assert design.stitching == ()

    @needs_kicad_libraries
    def test_a_design_without_pours_emits_no_zone(self, tmp_path: Path) -> None:
        source = (REPO_ROOT / "examples" / "congestion" / "design.yaml").read_text()
        assert "pours:" not in source, "congestion has no ground net, so no pour"
        result = build_design(
            REPO_ROOT / "examples" / "congestion" / "design.yaml", out_dir=tmp_path
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text()
        )
        assert not list(board.children("zone"))


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------


def zones_for(design: Path) -> list[SNode]:
    netlist = compile_netlist(design, Report())
    frame = frame_for(netlist)
    assert frame is not None
    return zone_nodes(netlist, frame, net_codes(sorted(netlist.nets)))


@needs_kicad_libraries
class TestZoneEmission:
    def test_a_zone_is_emitted_unfilled(self) -> None:
        """M10b's stability policy, as a property of the emitter."""
        for zone in zones_for(USB_PORT):
            assert not list(zone.children("filled_polygon"))
            assert zone.child("fill") is not None

    def test_the_zone_carries_the_pour_s_layers_and_net(self) -> None:
        zone = zones_for(USB_PORT)[0]
        assert zone.get("net_name") == "GND"
        assert [a.value for a in zone.child("layers").atoms()] == ["F.Cu", "B.Cu"]

    def test_the_uuid_is_derived_from_the_pour_s_position(self) -> None:
        zone = zones_for(USB_PORT)[0]
        assert zone.get("uuid") == zone_uuid(0)
        assert zone_uuid(0) != zone_uuid(1)

    def test_a_region_pour_carries_its_own_polygon(self) -> None:
        zones = zones_for(MCU4)
        board = zones[0].child("polygon").child("pts")
        region = zones[2].child("polygon").child("pts")
        assert len(list(region.children("xy"))) == 4
        assert [n.value(0) for n in board.children("xy")] != [
            n.value(0) for n in region.children("xy")
        ]

    def test_priority_is_written_only_when_it_is_not_the_default(self) -> None:
        zones = zones_for(MCU4)
        assert zones[0].child("priority") is None
        assert zones[2].child("priority").value(0) == "1"

    def test_thermal_and_solid_are_different_connect_pads_nodes(self) -> None:
        thermal = _zone_from(Pour.model_validate({"net": "GND", "layer": "F.Cu"}))
        solid = _zone_from(
            Pour.model_validate({"net": "GND", "layer": "F.Cu", "connect": "solid"})
        )
        assert not thermal.child("connect_pads").atoms()
        assert [a.value for a in solid.child("connect_pads").atoms()] == ["yes"]

    def test_island_removal_maps_onto_kicad_s_modes(self) -> None:
        # `always` is KiCad's own default and mode 0, so it is left unwritten.
        always = _zone_from(Pour.model_validate({"net": "GND", "layer": "F.Cu"}))
        assert always.child("fill").child("island_removal_mode") is None
        never = _zone_from(
            Pour.model_validate(
                {"net": "GND", "layer": "F.Cu", "remove_islands": "never"}
            )
        )
        assert never.child("fill").child("island_removal_mode").value(0) == "1"
        below = _zone_from(
            Pour.model_validate(
                {
                    "net": "GND",
                    "layer": "F.Cu",
                    "remove_islands": "below_area",
                    "island_area_min": 3.0,
                }
            )
        )
        assert below.child("fill").child("island_removal_mode").value(0) == "2"
        assert below.child("fill").child("island_area_min").value(0) == "3"

    def test_hatch_parameters_are_passed_through(self) -> None:
        zone = _zone_from(
            Pour.model_validate(
                {
                    "net": "GND",
                    "layer": "F.Cu",
                    "hatch": {"thickness": 0.4, "gap": 0.6, "orientation": 45},
                }
            )
        )
        fill = zone.child("fill")
        assert [a.value for a in fill.child("mode").atoms()] == ["hatch"]
        assert fill.child("hatch_thickness").value(0) == "0.4"
        assert fill.child("hatch_gap").value(0) == "0.6"
        assert fill.child("hatch_orientation").value(0) == "45"


def _zone_from(pour: Pour) -> SNode:
    """One zone, built through the real emitter on a minimal netlist."""
    from aipcb.compile.frame import frame_from_board
    from aipcb.model.board import Board

    netlist = compile_netlist.__globals__["Netlist"](
        name="t", revision="A", components={}, nets={}, pours=(pour,)
    )
    netlist.board = Board.model_validate({"outline": {"rect": [20.0, 20.0]}})
    frame = frame_from_board(netlist.board, (100.0, 100.0))
    return zone_nodes(netlist, frame, {"GND": 1})[0]


@needs_kicad_libraries
class TestKeepoutZones:
    """A pour must not put copper where the source said nothing may go.

    No bundled example exercises this: the only two designs with `keepouts:` are
    `congestion` and `overconstrained`, which have no ground net and therefore no
    pour. So it is tested on a design written for it.
    """

    def _probe(self, tmp_path: Path) -> Path:
        library = REPO_ROOT / "examples" / "library" / "passives.yaml"
        path = tmp_path / "design.yaml"
        path.write_text(
            f"""
name: keepout-probe
libraries:
  - {library}
nets:
  GND: {{class: ground}}
  SIG: {{class: signal}}
components:
  R1: {{part: R_10K_0603, pins: {{"1": SIG, "2": GND}}}}
  R2: {{part: R_10K_0603, pins: {{"1": SIG, "2": GND}}}}
board:
  outline: {{rect: [30.0, 30.0]}}
placement:
  R1: {{fixed: {{x: 6.0, y: 15.0}}, reason: probe}}
  R2: {{fixed: {{x: 24.0, y: 15.0}}, reason: probe}}
layout:
  placement:
    keepouts:
      - region_mm: [12.0, 8.0, 20.0, 22.0]
        reason: antenna clearance
pours:
  - net: GND
    layers: [F.Cu, B.Cu]
""".lstrip(),
            encoding="utf-8",
        )
        return path

    def test_a_keepout_becomes_a_zone_that_excludes_pour(self, tmp_path: Path) -> None:
        result = build_design(self._probe(tmp_path), out_dir=tmp_path / "b")
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text()
        )
        keepouts = [z for z in board.children("zone") if z.child("keepout") is not None]
        assert len(keepouts) == 1
        block = keepouts[0].child("keepout")
        assert block.child("copperpour").value(0) == "not_allowed"
        assert keepouts[0].get("net_name") == ""

    def test_a_design_with_keepouts_but_no_pour_emits_none(self) -> None:
        """Backward compatibility: `congestion` has keepouts and must not change."""
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "congestion" / "design.yaml", Report()
        )
        assert netlist.layout is not None
        assert netlist.layout.placement.keepouts, "congestion declares keepouts"
        assert not netlist.pours
        frame = frame_for(netlist)
        assert frame is not None
        assert zone_nodes(netlist, frame, net_codes(sorted(netlist.nets))) == []

    @needs_kicad_cli
    @HAS_PCBNEW
    def test_the_fill_leaves_the_keepout_empty(self, tmp_path: Path) -> None:
        """Read back off the filled board: no copper in the region, at all."""
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.geometry import box
        from shapely.ops import unary_union

        from aipcb.kicad.fill import fill_project

        result = build_design(self._probe(tmp_path), out_dir=tmp_path / "b")
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        filled, _ = fill_project(board_path, tmp_path / "f")
        board = parse(filled.read_text(encoding="utf-8"))
        pour = next(z for z in board.children("zone") if z.child("keepout") is None)
        copper = unary_union(
            [
                ShapelyPolygon(
                    [
                        (float(xy.value(0)), float(xy.value(1)))
                        for xy in polygon.child("pts").children("xy")
                    ]
                ).buffer(0)
                for polygon in pour.children("filled_polygon")
            ]
        )
        origin = result.netlist.layout.origin_mm
        region = box(
            origin[0] + 12.0, origin[1] + 8.0, origin[0] + 20.0, origin[1] + 22.0
        )
        assert copper.intersection(region).area == pytest.approx(0.0, abs=1e-6)
        assert copper.area > 0, "the rest of the board should still be poured"


# ---------------------------------------------------------------------------
# per-pad-instance keying -- the defect this has to survive
# ---------------------------------------------------------------------------


def _footprint_with_shared_numbers() -> SNode:
    """A footprint whose pads deliberately share a number, as a receptacle's do."""
    node = SNode("footprint").add(quoted("Test:Shared"))
    for number in ("1", "6", "6", "6"):
        node.add(
            SNode("pad").add(
                quoted(number),
                sym("smd"),
                sym("rect"),
                SNode("at").add(num(0), num(0)),
                SNode("size").add(num(1), num(1)),
            )
        )
    return node


class TestPerPadInstanceKeying:
    def test_the_override_lands_on_one_instance_of_a_shared_number(self) -> None:
        node = _footprint_with_shared_numbers()
        applied = apply_pad_connect(node, "J1", {"J1.6#2": 2})
        assert applied == 1
        values = [
            (p.value(0), p.child("zone_connect").value(0) if p.child("zone_connect") else None)
            for p in node.children("pad")
        ]
        assert values == [("1", None), ("6", None), ("6", "2"), ("6", None)]

    def test_no_suffix_means_every_pad_carrying_that_number(self) -> None:
        """Both forms are needed: "flood all four shield tabs" and "flood this one"."""
        node = _footprint_with_shared_numbers()
        applied = apply_pad_connect(node, "J1", {"J1.6": 1})
        assert applied == 3
        marked = [
            i for i, p in enumerate(node.children("pad")) if p.child("zone_connect")
        ]
        assert marked == [1, 2, 3]

    def test_an_instance_suffix_outranks_the_bare_number(self) -> None:
        node = _footprint_with_shared_numbers()
        apply_pad_connect(node, "J1", {"J1.6": 1, "J1.6#2": 2})
        values = [
            p.child("zone_connect").value(0) if p.child("zone_connect") else None
            for p in node.children("pad")
        ]
        assert values == [None, "1", "2", "1"]

    def test_pad_zone_connect_reads_every_pour(self) -> None:
        netlist = compile_netlist.__globals__["Netlist"](
            name="t",
            revision="A",
            components={},
            nets={},
            pours=(
                Pour.model_validate(
                    {
                        "net": "GND",
                        "layer": "F.Cu",
                        "pad_connect": [
                            {"pads": ["U1.33"], "connect": "solid"},
                            {"pads": ["U1.1"], "connect": "none"},
                        ],
                    }
                ),
            ),
        )
        assert pad_zone_connect(netlist) == {"U1.33": 2, "U1.1": 0}

    @needs_kicad_libraries
    def test_the_example_marks_exactly_one_of_twelve_identical_pads(
        self, tmp_path: Path
    ) -> None:
        """The defect in `docs/roadmap.md`, verified not to block this feature.

        `usb-port`'s receptacle emits twelve pads numbered 6 that share one UUID.
        The source names `J1.6#7`; exactly that pad, and no other, must come back
        with a `zone_connect` token.
        """
        result = build_design(USB_PORT, out_dir=tmp_path)
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text()
        )
        footprint = next(
            fp
            for fp in board.children("footprint")
            if any(
                p.value(0) == "Reference" and p.value(1) == "J1"
                for p in fp.children("property")
            )
        )
        pads = [p for p in footprint.children("pad") if p.value(0) == "6"]
        assert len(pads) == 12
        assert len({p.get("uuid") for p in pads}) == 1, (
            "the shared-UUID defect is expected to still be there; if it is fixed, "
            "this test has served its purpose and the roadmap entry can go"
        )
        marked = [i for i, p in enumerate(pads) if p.child("zone_connect") is not None]
        assert marked == [6], "only the seventh pad numbered 6 was named"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def check_source(text: str, write_design) -> Report:
    report = Report()
    netlist = elaborate(load_design(write_design(text), report=report), report)
    fresh = Report()
    run_pour_checks(netlist, fresh)
    return fresh


BASE = """
name: pourtest
components:
  R1:
    part: R_10K_0603
    pins: {"1": VCC, "2": GND}
  R2:
    part: R_10K_0603
    pins: {"1": VCC, "2": GND}
board:
  outline:
    rect: [20.0, 20.0]
"""


@needs_kicad_libraries
class TestPourValidation:
    def _codes(self, text: str, write_design) -> list[str]:
        return [d.code for d in check_source(BASE + text, write_design)]

    def test_a_pour_on_an_unknown_net_is_an_error(self, write_design) -> None:
        assert "pour-unknown-net" in self._codes(
            "pours:\n  - net: NOPE\n    layer: F.Cu\n", write_design
        )

    def test_a_pour_on_a_layer_the_stackup_lacks_is_an_error(self, write_design) -> None:
        assert "pour-unknown-layer" in self._codes(
            "pours:\n  - net: GND\n    layer: In2.Cu\n", write_design
        )

    def test_a_region_outside_the_outline_is_an_error(self, write_design) -> None:
        assert "pour-region-outside-board" in self._codes(
            "pours:\n  - net: GND\n    layer: F.Cu\n"
            "    region: {rect: [[80, 80], [90, 90]]}\n",
            write_design,
        )

    def test_a_region_crossing_the_edge_is_a_warning(self, write_design) -> None:
        assert "pour-region-crosses-edge" in self._codes(
            "pours:\n  - net: GND\n    layer: F.Cu\n"
            "    region: {rect: [[10, 10], [30, 30]]}\n",
            write_design,
        )

    def test_overlapping_zones_on_one_layer_need_distinct_priorities(
        self, write_design
    ) -> None:
        codes = self._codes(
            "pours:\n"
            "  - net: GND\n    layer: F.Cu\n"
            "  - net: VCC\n    layer: F.Cu\n",
            write_design,
        )
        assert "pour-priority-tie" in codes

    def test_distinct_priorities_are_fine(self, write_design) -> None:
        codes = self._codes(
            "pours:\n"
            "  - net: GND\n    layer: F.Cu\n    priority: 0\n"
            "  - net: VCC\n    layer: F.Cu\n    priority: 1\n",
            write_design,
        )
        assert "pour-priority-tie" not in codes

    def test_zones_that_do_not_overlap_may_share_a_priority(self, write_design) -> None:
        codes = self._codes(
            "pours:\n"
            "  - net: GND\n    layer: F.Cu\n"
            "    region: {rect: [[1, 1], [8, 8]]}\n"
            "  - net: VCC\n    layer: F.Cu\n"
            "    region: {rect: [[12, 12], [19, 19]]}\n",
            write_design,
        )
        assert "pour-priority-tie" not in codes

    def test_a_pad_override_naming_no_component_is_an_error(self, write_design) -> None:
        assert "pour-unknown-pad" in self._codes(
            "pours:\n  - net: GND\n    layer: F.Cu\n"
            "    pad_connect:\n      - pads: [U9.1]\n        connect: solid\n",
            write_design,
        )

    def test_stitching_without_a_pour_to_reach_is_a_warning(self, write_design) -> None:
        assert "stitching-without-pour" in self._codes(
            "stitching:\n  - net: GND\n    pattern: grid\n    pitch: 2.0\n",
            write_design,
        )

    def test_a_ring_around_nothing_is_an_error(self, write_design) -> None:
        assert "stitching-unknown-part" in self._codes(
            "pours:\n  - net: GND\n    layers: [F.Cu, B.Cu]\n"
            "stitching:\n  - net: GND\n    pattern: ring\n    pitch: 2.0\n"
            "    around: U9\n",
            write_design,
        )


# ---------------------------------------------------------------------------
# the fill subprocess
# ---------------------------------------------------------------------------


class TestVersionLock:
    def test_a_debian_revision_is_the_same_version(self) -> None:
        assert version_number("9.0.8+dfsg-1") == "9.0.8"
        assert same_version("9.0.8", "9.0.8+dfsg-1")

    def test_a_different_build_is_not(self) -> None:
        assert not same_version("9.0.8", "8.0.6")
        assert not same_version("9.0.8", "9.0.9")

    def test_an_unreadable_version_never_counts_as_a_match(self) -> None:
        assert not same_version("", "9.0.8")
        assert not same_version("9.0.8", "unknown")

    @needs_kicad_cli
    @HAS_PCBNEW
    def test_a_mismatch_stops_the_fill_and_names_both(self, tmp_path: Path) -> None:
        board = tmp_path / "b.kicad_pcb"
        board.write_text("(kicad_pcb)\n", encoding="utf-8")
        with pytest.raises(FillError) as caught:
            fill_board(board, tmp_path / "o.kicad_pcb", kicad_version="8.0.6")
        message = str(caught.value)
        assert "8.0.6" in message
        found = find_pcbnew_python()
        assert found is not None
        assert version_number(found.version) in message
        assert not (tmp_path / "o.kicad_pcb").exists(), "nothing may be written"

    @needs_kicad_cli
    @HAS_PCBNEW
    def test_the_script_refuses_a_mismatch_on_its_own(self, tmp_path: Path) -> None:
        """The lock lives in the subprocess too, not only in its caller."""
        found = find_pcbnew_python()
        assert found is not None
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT / "src")
        run = subprocess.run(
            [
                found.executable, "-m", "aipcb.kicad.fill",
                str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"),
                "--require-version", "1.2.3",
            ],
            capture_output=True, text=True, check=False, env=environment,
        )
        assert run.returncode == 4
        assert "1.2.3" in run.stderr
        assert "refusing to fill" in run.stderr


class TestPcbnewAbsent:
    def test_the_failure_is_loud_and_says_what_to_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 0009's fourth condition: simulate `pcbnew` being absent.

        An interpreter that cannot import it is pointed at deliberately, so the
        message the user would get is the message this asserts.
        """
        stub = tmp_path / "python3"
        stub.write_text(
            "#!/bin/sh\nexit 1\n", encoding="utf-8"
        )
        stub.chmod(0o755)
        monkeypatch.setenv(PCBNEW_PYTHON_ENV, str(stub))
        find_pcbnew_python(refresh=True)
        try:
            board = tmp_path / "b.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            with pytest.raises(FillError) as caught:
                fill_board(board, tmp_path / "o.kicad_pcb", kicad_version="9.0.8")
            message = str(caught.value)
            assert "import pcbnew" in message
            assert PCBNEW_PYTHON_ENV in message
            assert not (tmp_path / "o.kicad_pcb").exists()
        finally:
            monkeypatch.delenv(PCBNEW_PYTHON_ENV)
            find_pcbnew_python(refresh=True)

    @needs_kicad_libraries
    @needs_kicad_cli
    def test_a_check_reports_it_rather_than_checking_an_unfilled_board(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The silent-corruption path, closed: no fill means no DRC verdict."""
        from aipcb.checks.loop import check_design

        stub = tmp_path / "python3"
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
        monkeypatch.setenv(PCBNEW_PYTHON_ENV, str(stub))
        find_pcbnew_python(refresh=True)
        try:
            report = Report()
            result = check_design(
                USB_PORT, out_dir=tmp_path / "work", report=report, route=False
            )
            codes = [d.code for d in report]
            assert "zone-fill-failed" in codes
            assert not result.drc.ran, "DRC must not run over an unfilled pour"
            assert not report.ok
        finally:
            monkeypatch.delenv(PCBNEW_PYTHON_ENV)
            find_pcbnew_python(refresh=True)


# ---------------------------------------------------------------------------
# plane integrity, on boards built by hand so the answer is known
# ---------------------------------------------------------------------------


def _board_with_pour(*filled: list[tuple[float, float]]) -> SNode:
    """A 20 x 20 board carrying one GND zone with the given filled polygons."""
    board = SNode("kicad_pcb")
    corners = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    for index in range(4):
        a, b = corners[index], corners[(index + 1) % 4]
        board.add(
            SNode("gr_line").add(
                SNode("start").add(num(a[0]), num(a[1])),
                SNode("end").add(num(b[0]), num(b[1])),
                SNode("layer").add(quoted("Edge.Cuts")),
            )
        )
    zone = SNode("zone").add(
        SNode("net").add(num(1)),
        SNode("net_name").add(quoted("GND")),
        SNode("layer").add(quoted("F.Cu")),
        SNode("uuid").add(quoted(zone_uuid(0))),
    )
    zone.add(
        SNode("polygon").add(
            SNode("pts").add(*(SNode("xy").add(num(x), num(y)) for x, y in corners))
        )
    )
    for polygon in filled:
        zone.add(
            SNode("filled_polygon").add(
                SNode("layer").add(quoted("F.Cu")),
                SNode("pts").add(
                    *(SNode("xy").add(num(x), num(y)) for x, y in polygon)
                ),
            )
        )
    board.add(zone)
    return board


def _netlist_with_pour(**pour: object):
    from aipcb.model.board import Board
    from aipcb.netlist import Netlist

    netlist = Netlist(
        name="t",
        revision="A",
        components={},
        nets={},
        pours=(Pour.model_validate({"net": "GND", "layer": "F.Cu", **pour}),),
    )
    netlist.board = Board.model_validate({"outline": {"rect": [20.0, 20.0]}})
    return netlist


class TestPlaneIntegrity:
    def test_one_piece_of_copper_is_one_island(self) -> None:
        board = _board_with_pour([(1, 1), (19, 1), (19, 19), (1, 19)])
        plane = analyse_planes(board, _netlist_with_pour())[0]
        assert plane.islands == 1
        assert plane.layers[0].contiguous == pytest.approx(1.0)
        assert plane.layers[0].largest_mm2 == pytest.approx(18 * 18)

    def test_a_track_slicing_a_pour_gives_two_islands(self) -> None:
        """The M10d acceptance case, with an island count that can be checked.

        A 20 x 20 pour cut across the middle: 18 x 8 above the cut, 18 x 4 below.
        The largest holds 18*8 / (18*8 + 18*4) = two thirds of the copper.
        """
        board = _board_with_pour(
            [(1, 1), (19, 1), (19, 9), (1, 9)],
            [(1, 11), (19, 11), (19, 15), (1, 15)],
        )
        plane = analyse_planes(board, _netlist_with_pour())[0]
        layer = plane.layers[0]
        assert len(layer.islands) == 2
        assert layer.largest_mm2 == pytest.approx(18 * 8)
        assert layer.filled_mm2 == pytest.approx(18 * 8 + 18 * 4)
        assert layer.contiguous == pytest.approx(2 / 3)
        assert layer.scope_mm2 == pytest.approx(400.0)
        assert layer.coverage == pytest.approx(144 / 400)

    def test_island_bounding_boxes_are_reported_largest_first(self) -> None:
        board = _board_with_pour(
            [(1, 11), (19, 11), (19, 15), (1, 15)],
            [(1, 1), (19, 1), (19, 9), (1, 9)],
        )
        plane = analyse_planes(board, _netlist_with_pour())[0]
        boxes = [island.bbox for island in plane.layers[0].islands]
        assert boxes == [(1.0, 1.0, 19.0, 9.0), (1.0, 11.0, 19.0, 15.0)]

    def test_min_contiguous_turns_fragmentation_into_a_warning(self) -> None:
        board = _board_with_pour(
            [(1, 1), (19, 1), (19, 9), (1, 9)],
            [(1, 11), (19, 11), (19, 15), (1, 15)],
        )
        netlist = _netlist_with_pour(min_contiguous=0.9)
        planes = analyse_planes(board, netlist)
        assert planes[0].fragmented
        report = Report()
        from aipcb.checks.planes import report_planes

        report_planes(planes, netlist, report)
        warnings = [d for d in report if d.severity is Severity.WARNING]
        assert [d.code for d in warnings] == ["plane-fragmented"]
        assert warnings[0].path == ("pours", 0)

    def test_without_a_threshold_fragmentation_is_only_a_note(self) -> None:
        board = _board_with_pour(
            [(1, 1), (19, 1), (19, 9), (1, 9)],
            [(1, 11), (19, 11), (19, 15), (1, 15)],
        )
        netlist = _netlist_with_pour()
        report = Report()
        from aipcb.checks.planes import report_planes

        report_planes(analyse_planes(board, netlist), netlist, report)
        assert not report.errors
        assert not report.warnings
        assert [d.code for d in report] == ["plane-integrity"]

    def test_the_analysis_is_a_pure_function(self) -> None:
        board = _board_with_pour([(1, 1), (19, 1), (19, 19), (1, 19)])
        netlist = _netlist_with_pour()
        first = analyse_planes(board, netlist)[0].to_dict()
        second = analyse_planes(board, netlist)[0].to_dict()
        assert first == second


# ---------------------------------------------------------------------------
# stitching
# ---------------------------------------------------------------------------


def routed_board(design: Path, out: Path) -> tuple[SNode, object]:
    report = Report()
    result = build_design(design, out_dir=out, report=report)
    path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    tree = parse(path.read_text(encoding="utf-8"))
    topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
    routed = route_board(tree, result.netlist, report, topologies=topologies)
    attach_copper(tree, routed.connections, sorted(result.netlist.nets))
    return tree, result.netlist


@needs_kicad_libraries
class TestStitching:
    def test_the_grid_and_edge_patterns_both_place_vias(self, tmp_path: Path) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        result = stitch_board(tree, netlist, Report())
        assert len(result.candidates) == 2, "usb-port declares a grid and an edge row"
        assert all(count > 0 for count in result.candidates.values())
        assert {via.pattern for via in result.placed} == {0, 1}

    def test_skipped_positions_are_counted_not_hidden(self, tmp_path: Path) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        result = stitch_board(tree, netlist, Report())
        assert result.total_skipped > 0, "a routed board blocks some positions"
        for index, candidates in result.candidates.items():
            placed = sum(1 for v in result.placed if v.pattern == index)
            assert placed + result.skipped[index] == candidates

    def test_positions_are_deterministic_across_runs(self, tmp_path: Path) -> None:
        first, netlist = routed_board(USB_PORT, tmp_path / "a")
        second, _ = routed_board(USB_PORT, tmp_path / "b")
        a = stitch_board(first, netlist, Report())
        b = stitch_board(second, netlist, Report())
        assert [(v.uuid, v.point) for v in a.placed] == [
            (v.uuid, v.point) for v in b.placed
        ]

    def test_uuids_are_derived_from_the_pattern_and_the_position_in_it(self) -> None:
        assert stitch_uuid(0, 3) == stitch_uuid(0, 3)
        assert stitch_uuid(0, 3) != stitch_uuid(1, 3)
        assert stitch_uuid(0, 3) != stitch_uuid(0, 4)

    def test_a_second_run_replaces_its_own_vias_rather_than_adding_more(
        self, tmp_path: Path
    ) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        first = stitch_board(tree, netlist, Report())
        before = dump(tree)
        second = stitch_board(tree, netlist, Report())
        assert len(second.placed) == len(first.placed)
        assert dump(tree) == before

    def test_every_via_keeps_clear_of_every_hole_on_the_board(
        self, tmp_path: Path
    ) -> None:
        """The `mcu-4layer` defect: same-net copper may touch, holes may not."""
        tree, netlist = routed_board(MCU4, tmp_path)
        placed = stitch_board(tree, netlist, Report()).placed
        assert placed
        holes: list[tuple[tuple[float, float], float]] = []
        for via in tree.children("via"):
            at = via.child("at")
            drill = via.child("drill")
            holes.append(
                (
                    (float(at.value(0)), float(at.value(1))),
                    float(drill.value(0)) if drill is not None else 0.3,
                )
            )
        for index, (centre, drill) in enumerate(holes):
            for other, other_drill in holes[index + 1 :]:
                gap = math.dist(centre, other) - (drill + other_drill) / 2
                assert gap >= 0.2495, f"{centre} and {other} are {gap:.4f} mm apart"

    def test_every_via_lands_inside_the_pour_on_both_layers(
        self, tmp_path: Path
    ) -> None:
        from shapely.geometry import Point as ShapelyPoint
        from shapely.geometry import Polygon as ShapelyPolygon

        tree, netlist = routed_board(USB_PORT, tmp_path)
        placed = stitch_board(tree, netlist, Report()).placed
        zone = next(tree.children("zone"))
        polygon = ShapelyPolygon(
            [
                (float(xy.value(0)), float(xy.value(1)))
                for xy in zone.child("polygon").child("pts").children("xy")
            ]
        )
        for via in placed:
            assert polygon.contains(ShapelyPoint(via.point))

    def test_a_ring_pattern_circles_the_part_it_names(self, tmp_path: Path) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        netlist.stitching = (
            Stitching.model_validate(
                {
                    "net": "GND",
                    "pattern": "ring",
                    "around": "R3",
                    "pitch": 2.0,
                    "radius": 3.0,
                }
            ),
        )
        placed = stitch_board(tree, netlist, Report()).placed
        assert placed, "a ring round a 0603 in open copper should place something"
        centre = next(
            (float(fp.child("at").value(0)), float(fp.child("at").value(1)))
            for fp in tree.children("footprint")
            if any(
                p.value(0) == "Reference" and p.value(1) == "R3"
                for p in fp.children("property")
            )
        )
        for via in placed:
            assert math.dist(via.point, centre) == pytest.approx(3.0, abs=1e-6)

    def test_stitching_without_a_pour_places_nothing_and_says_so(
        self, tmp_path: Path
    ) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        netlist.stitching = (
            Stitching.model_validate({"net": "VBUS", "pattern": "grid", "pitch": 4.0}),
        )
        report = Report()
        result = stitch_board(tree, netlist, report)
        assert not result.placed
        assert "stitching-no-plane" in [d.code for d in report]

    def test_a_via_is_an_ordinary_via_in_the_output(self, tmp_path: Path) -> None:
        tree, netlist = routed_board(USB_PORT, tmp_path)
        before = len(list(tree.children("via")))
        result = stitch_board(tree, netlist, Report())
        after = list(tree.children("via"))
        assert len(after) == before + len(result.placed)
        stitched = next(v for v in after if v.get("uuid") == result.placed[0].uuid)
        assert stitched.child("at") is not None
        assert stitched.child("drill") is not None
        assert [a.value for a in stitched.child("layers").atoms()] == ["F.Cu", "B.Cu"]


# ---------------------------------------------------------------------------
# the filled board -- read back, never trusted
# ---------------------------------------------------------------------------


def _fill(design: Path, tmp_path: Path) -> tuple[SNode, Path]:
    """Build, route, stitch and fill one example. Returns the filled tree."""
    from aipcb.kicad.fill import fill_project

    out = tmp_path / "build"
    tree, netlist = routed_board(design, out)
    stitch_board(tree, netlist, Report())
    board_path = next(out.glob("*.kicad_pcb"))
    board_path.write_text(dump(tree), encoding="utf-8")
    filled, _ = fill_project(board_path, tmp_path / "filled")
    return parse(filled.read_text(encoding="utf-8")), filled


def _copper(zone: SNode, layer: str):
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union

    shapes = []
    for filled in zone.children("filled_polygon"):
        if filled.get("layer") != layer:
            continue
        points = [
            (float(xy.value(0)), float(xy.value(1)))
            for xy in filled.child("pts").children("xy")
        ]
        if len(points) >= 3:
            shape = ShapelyPolygon(points)
            shapes.append(shape if shape.is_valid else shape.buffer(0))
    return unary_union(shapes)


def _pad_instances(board: SNode, refdes: str) -> list[tuple[str, SNode, tuple[float, float]]]:
    """Every pad of a footprint, keyed per instance, with its board position."""
    footprint = next(
        fp
        for fp in board.children("footprint")
        if any(
            p.value(0) == "Reference" and p.value(1) == refdes
            for p in fp.children("property")
        )
    )
    at = footprint.child("at")
    fx, fy = float(at.value(0)), float(at.value(1))
    rotation = math.radians(float(at.value(2) or 0))
    cos, sin = math.cos(rotation), math.sin(rotation)
    seen: dict[str, int] = {}
    out = []
    for pad in footprint.children("pad"):
        number = pad.value(0)
        seen[number] = seen.get(number, 0) + 1
        key = f"{refdes}.{number}" if seen[number] == 1 else f"{refdes}.{number}#{seen[number]}"
        pad_at = pad.child("at")
        px, py = float(pad_at.value(0) or 0), float(pad_at.value(1) or 0)
        out.append((key, pad, (px * cos + py * sin + fx, -px * sin + py * cos + fy)))
    return out


def _ring_coverage(copper, centre: tuple[float, float], radius: float) -> float:
    """What fraction of a circle just outside a pad is covered by poured copper.

    A solid connection floods the pad, so the ring is entirely inside the copper.
    A thermal relief leaves an annular gap broken only by its spokes, so most of
    the ring is *outside*. This is how the difference is read off the geometry
    rather than off the parameter that asked for it.
    """
    from shapely.geometry import Point as ShapelyPoint

    steps = 360
    inside = 0
    for step in range(steps):
        angle = math.tau * step / steps
        point = ShapelyPoint(
            centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle)
        )
        if copper.contains(point):
            inside += 1
    return inside / steps


def _thermal_probe(tmp_path: Path) -> Path:
    """Two identical GND pads, far enough apart that nothing else touches them."""
    library = REPO_ROOT / "examples" / "library" / "passives.yaml"
    path = tmp_path / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
name: thermal-probe
libraries:
  - {library}
nets:
  GND: {{class: ground}}
  SIG: {{class: signal}}
components:
  R1: {{part: R_10K_0603, pins: {{"1": SIG, "2": GND}}}}
  R2: {{part: R_10K_0603, pins: {{"1": SIG, "2": GND}}}}
board:
  outline: {{rect: [30.0, 30.0]}}
placement:
  R1: {{fixed: {{x: 9.0, y: 15.0}}, reason: far from anything else}}
  R2: {{fixed: {{x: 21.0, y: 15.0}}, reason: far from anything else}}
pours:
  - net: GND
    layer: F.Cu
    connect: thermal
    pad_connect:
      - pads: [R1.2]
        connect: solid
        reason: the pad this test floods
""".lstrip(),
        encoding="utf-8",
    )
    return path


@needs_kicad_libraries
@needs_kicad_cli
@HAS_PCBNEW
class TestFilledBoard:
    def test_the_zones_come_back_filled(self, tmp_path: Path) -> None:
        board, _ = _fill(USB_PORT, tmp_path)
        zone = next(board.children("zone"))
        assert list(zone.children("filled_polygon"))

    def test_build_output_stays_unfilled(self, tmp_path: Path) -> None:
        """The stability policy as a directory: the fill happens in a copy."""
        _fill(USB_PORT, tmp_path)
        built = parse((tmp_path / "build" / "usb-port.kicad_pcb").read_text())
        zone = next(built.children("zone"))
        assert not list(zone.children("filled_polygon"))

    def test_a_thermal_pad_has_spokes_and_a_solid_one_does_not(
        self, tmp_path: Path
    ) -> None:
        """M10's thermal-relief acceptance, counted off the filled copper.

        Two identical 0603 pads on one net, one poured thermally and one flooded
        by a `pad_connect` override. Around a thermally relieved pad the fill
        leaves an annular gap broken by its spokes, so the *gaps* in that annulus
        are countable: four of them means four spokes. A solid connection leaves
        none, because the copper runs right up to the pad all the way round.

        Nothing here reads the parameter that asked for the difference.
        """
        from shapely.geometry import MultiPolygon
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.ops import unary_union

        from aipcb.kicad.fill import fill_project

        design = _thermal_probe(tmp_path)
        result = build_design(design, out_dir=tmp_path / "b", report=Report())
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        filled, _ = fill_project(board_path, tmp_path / "f")
        board = parse(filled.read_text(encoding="utf-8"))

        zone = next(board.children("zone"))
        copper = unary_union(
            [
                ShapelyPolygon(
                    [
                        (float(xy.value(0)), float(xy.value(1)))
                        for xy in polygon.child("pts").children("xy")
                    ]
                ).buffer(0)
                for polygon in zone.children("filled_polygon")
            ]
        )

        def relief(refdes: str) -> tuple[int, SNode]:
            """How many gaps the annulus round this pad has, and the pad node."""
            footprint = next(
                fp
                for fp in board.children("footprint")
                if any(
                    q.value(0) == "Reference" and q.value(1) == refdes
                    for q in fp.children("property")
                )
            )
            at = footprint.child("at")
            fx, fy = float(at.value(0)), float(at.value(1))
            pad = next(p for p in footprint.children("pad") if p.value(0) == "2")
            pad_at, size = pad.child("at"), pad.child("size")
            cx = float(pad_at.value(0) or 0) + fx
            cy = float(pad_at.value(1) or 0) + fy
            w, h = float(size.value(0)), float(size.value(1))
            rect = ShapelyPolygon(
                [
                    (cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                    (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2),
                ]
            )
            annulus = rect.buffer(0.25, join_style="mitre").difference(rect)
            gaps = annulus.difference(copper)
            pieces = list(gaps.geoms) if isinstance(gaps, MultiPolygon) else [gaps]
            return len([p for p in pieces if not p.is_empty and p.area > 1e-6]), pad

        solid_gaps, solid_pad = relief("R1")
        thermal_gaps, thermal_pad = relief("R2")

        assert solid_pad.child("zone_connect").value(0) == "2"
        assert thermal_pad.child("zone_connect") is None
        assert thermal_gaps == 4, (
            f"a thermal relief should leave four gaps between four spokes, "
            f"found {thermal_gaps}"
        )
        assert solid_gaps == 0, (
            f"a solid connection should leave no gap round the pad, found "
            f"{solid_gaps}"
        )

    def test_the_solid_shield_tab_is_flooded_more_than_its_thermal_twin(
        self, tmp_path: Path
    ) -> None:
        """The same claim on the real example, where the pads share a number.

        `usb-port` floods `J1.6#7` and leaves the other eleven tabs thermal.
        `J1.6#6` is the same size and the mirror position, so comparing those two
        is comparing the override and nothing else.
        """
        from shapely.geometry import Polygon as ShapelyPolygon

        board, _ = _fill(USB_PORT, tmp_path)
        copper = _copper(next(board.children("zone")), "F.Cu")
        pads = {key: (pad, point) for key, pad, point in _pad_instances(board, "J1")}

        def flooded(key: str) -> float:
            pad, (cx, cy) = pads[key]
            size = pad.child("size")
            w, h = float(size.value(0)), float(size.value(1))
            rect = ShapelyPolygon(
                [
                    (cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                    (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2),
                ]
            )
            return copper.intersection(rect).area / rect.area

        solid, twin = pads["J1.6#7"], pads["J1.6#6"]
        assert solid[0].child("zone_connect").value(0) == "2"
        assert twin[0].child("zone_connect") is None
        assert solid[0].child("size").value(0) == twin[0].child("size").value(0)
        assert flooded("J1.6#7") > flooded("J1.6#6") + 0.2, (
            f"solid {flooded('J1.6#7'):.1%} against thermal twin "
            f"{flooded('J1.6#6'):.1%}"
        )

    def test_the_split_plane_fills_both_nets_and_both_stay_contiguous(
        self, tmp_path: Path
    ) -> None:
        """M10's split-plane acceptance, on `mcu-4layer`'s In2 layer."""
        board, _ = _fill(MCU4, tmp_path)
        netlist = compile_netlist(MCU4, Report())
        planes = {
            (plane.net, plane.pour): plane for plane in analyse_planes(board, netlist)
        }
        inner = [
            plane
            for plane in planes.values()
            if any(layer.layer == "In2.Cu" for layer in plane.layers)
        ]
        assert {plane.net for plane in inner} == {"VCC", "GND"}
        for plane in inner:
            layer = next(x for x in plane.layers if x.layer == "In2.Cu")
            assert len(layer.islands) == 1, f"{plane.net} on In2 is in pieces"
            assert layer.contiguous == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "name",
        ["diff-pair", "enclosure", "ldo-supply", "led-blinker", "mcu-4layer",
         "qfn-fanout", "routing-demo", "usb-port"],
    )
    def test_a_filled_example_has_no_drc_violations(
        self, name: str, tmp_path: Path
    ) -> None:
        """The acceptance bar: zero violations *after* the fill, not before it."""
        from aipcb.kicad.cli import run_kicad

        design = REPO_ROOT / "examples" / name / "design.yaml"
        _, filled = _fill(design, tmp_path)
        out = tmp_path / "drc.json"
        run = run_kicad(
            "pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
            "-o", str(out), str(filled),
        )
        assert run.returncode == 0, run.stderr
        payload = json.loads(out.read_text())
        assert not payload["violations"], [
            f"{v['severity']} {v['type']}: {v['description']}"
            for v in payload["violations"]
        ]
        assert not payload["unconnected_items"]
        assert not payload["schematic_parity"]

    def test_the_fill_geometry_is_deterministic(self, tmp_path: Path) -> None:
        """ADR 0009 Finding 3, re-measured on the code that ships.

        The *fill* repeats exactly. The whole file does not, because KiCad's
        writer invents UUIDs for the properties it adds -- which is why the
        stability guarantee covers build output and not the filled copy.
        """
        board, _filled = _fill(USB_PORT, tmp_path)
        first = [
            [(xy.value(0), xy.value(1)) for xy in polygon.child("pts").children("xy")]
            for zone in board.children("zone")
            for polygon in zone.children("filled_polygon")
        ]
        again, _ = _fill(USB_PORT, tmp_path / "again")
        second = [
            [(xy.value(0), xy.value(1)) for xy in polygon.child("pts").children("xy")]
            for zone in again.children("zone")
            for polygon in zone.children("filled_polygon")
        ]
        assert first == second
        assert first, "there should be copper to compare"

    def test_uuids_survive_the_fill_so_drc_still_maps_to_source(
        self, tmp_path: Path
    ) -> None:
        """The fill runs a different writer over the board; identity must survive."""
        board, _ = _fill(USB_PORT, tmp_path)
        built = parse((tmp_path / "build" / "usb-port.kicad_pcb").read_text())

        def uuids(node: SNode) -> set[str]:
            found: set[str] = set()
            for child in node.children():
                if child.name == "uuid" and child.value(0):
                    found.add(str(child.value(0)))
                found |= uuids(child)
            return found

        assert not uuids(built) - uuids(board), "the fill lost an element's identity"

    def test_the_zone_maps_back_to_the_pour_that_asked_for_it(self) -> None:
        netlist = compile_netlist(USB_PORT, Report())
        index = build_index(netlist)
        ref = index.lookup(zone_uuid(0))
        assert ref is not None
        assert ref.kind == "copper pour"
        assert ref.path == ("pours", 0)

    def test_a_stitching_via_maps_back_to_its_pattern(self) -> None:
        netlist = compile_netlist(USB_PORT, Report())
        index = build_index(netlist)
        for ordinal in (0, MAX_STITCH_VIAS - 1):
            ref = index.lookup(stitch_uuid(1, ordinal))
            assert ref is not None
            assert ref.path == ("stitching", 1)


@needs_kicad_libraries
@needs_kicad_cli
@HAS_PCBNEW
class TestExportShipsFilledCopper:
    def test_the_gerber_carries_the_plane(self, tmp_path: Path) -> None:
        """An unfilled pour exports as no copper at all, so this is measured."""
        from aipcb.compile.export import export_board
        from aipcb.kicad.cli import run_kicad

        report = Report()
        build = tmp_path / "build"
        result = build_design(USB_PORT, out_dir=build, report=report)
        board = next(p for p in result.written if p.suffix == ".kicad_pcb")

        bare = tmp_path / "bare"
        bare.mkdir()
        run_kicad(
            "pcb", "export", "gerbers", "--layers", "F.Cu", "--no-protel-ext",
            "-o", str(bare), str(board),
        )
        unfilled = next(bare.glob("*F_Cu*")).read_text()

        exported = export_board(
            board, tmp_path / "out", result.netlist, report, position=False
        )
        assert exported.ok, report.render()
        assert any(step.startswith("fill") for step in exported.steps)
        filled = next((tmp_path / "out").glob("*F_Cu*")).read_text()

        def records(text: str) -> int:
            return sum(1 for line in text.splitlines() if line.startswith("X"))

        assert records(filled) > 10 * records(unfilled), (
            f"filled {records(filled)} coordinate records against "
            f"{records(unfilled)} unfilled -- the pour should dominate"
        )
        assert (build / "usb-port.kicad_pcb").read_text().count("filled_polygon") == 0


def test_the_scratch_helpers_are_not_left_behind() -> None:
    """A guard against this file growing a dependency on the developer's machine."""
    assert shutil.which(sys.executable)
    assert Path(sys.executable).exists()

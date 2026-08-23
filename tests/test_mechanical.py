# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The mechanical layer: the board's boundary, and the parts the enclosure pins.

Three claims are under test here, and the first one is the one that would be
embarrassing to get wrong.

*The sign convention holds.* The source frame is Y up and KiCad's is Y down, and
the conversion happens in exactly one place. The test board is deliberately
asymmetric north to south, so a flipped conversion cannot come out looking right.

*The outline is a polygon, not a bounding box.* Placement, validation and routing
all reason about the real shape, holes included.

*`fixed` means fixed.* Nothing -- not a group, not the packer, not a hand nudge in
KiCad -- moves a part the source has pinned.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.frame import canonical_ring, frame_for, frame_from_board
from aipcb.compile.place import courtyard_box, plan_placement
from aipcb.diagnostics import Report, Severity
from aipcb.kicad.sexpr import SNode, dump, num, parse
from aipcb.model.board import Board, Line, Outline, ring_area, tessellate

from .conftest import REPO_ROOT, needs_kicad_libraries

LIBRARY = REPO_ROOT / "examples" / "library"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write_design(tmp_path: Path, body: str) -> Path:
    """Write a design that can reach the bundled part libraries."""
    (tmp_path / "library").symlink_to(LIBRARY)
    folder = tmp_path / "design"
    folder.mkdir()
    path = folder / "design.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def report_for(path: Path) -> Report:
    """Compile far enough to collect diagnostics, whether or not it gets there."""
    report = Report()
    with contextlib.suppress(Exception):
        compile_netlist(path, report)
    return report


def codes(report: Report, severity: Severity | None = None) -> set[str]:
    return {
        d.code for d in report if severity is None or d.severity is severity
    }


def board_of(design: Path, tmp_path: Path) -> SNode:
    build_design(design, out_dir=tmp_path)
    return parse(next(tmp_path.glob("*.kicad_pcb")).read_text(encoding="utf-8"))


def footprint_at(tree: SNode, refdes: str) -> tuple[float, float, float]:
    for fp in tree.children("footprint"):
        for prop in fp.children("property"):
            if prop.value(0) == "Reference" and prop.value(1) == refdes:
                at = fp.child("at")
                assert at is not None
                return (
                    float(at.value(0) or 0),
                    float(at.value(1) or 0),
                    float(at.value(2) or 0),
                )
    raise AssertionError(f"no footprint {refdes}")


def edges(tree: SNode, name: str = "gr_line") -> list[SNode]:
    return [g for g in tree.children(name) if g.get("layer") == "Edge.Cuts"]


#: A board whose north and south edges are different shapes, so that a Y flip in
#: the wrong direction cannot produce a plausible answer. The notch is at the top
#: in the source frame, which is the top of the board as KiCad draws it.
ASYMMETRIC = """
name: asym
libraries: [../library/passives.yaml, ../library/connectors.yaml]
nets:
  A: {class: signal, description: one}
  B: {class: signal, description: two}
components:
  J1:
    part: CONN_BRK_1X04
    role: connector
    reason: Something to place.
    pins: {P1: A, P2: A, P3: B, P4: B}
board:
  origin: bottom_left
  outline:
    polygon:
      - [0, 0]
      - [40, 0]
      - [40, 30]
      - [24, 30]
      - [24, 22]
      - [16, 22]
      - [16, 30]
      - [0, 30]
placement:
  J1:
    fixed: {x: 6, y: 25}
    reason: The corner nearest the notch, which is at the north.
layout:
  origin_mm: [100.0, 100.0]
  placement: {margin_mm: 1.0}
"""


# ---------------------------------------------------------------------------
# the sign convention
# ---------------------------------------------------------------------------


class TestSignConvention:
    """The Y flip, with a board that cannot look right upside down."""

    def test_source_y_up_becomes_kicad_y_down(self) -> None:
        frame = frame_from_board(
            Board(outline=Outline(rect=(80.0, 50.0))), (100.0, 100.0)
        )
        # The source's bottom-left is the board's bottom-left, which in KiCad -- Y
        # pointing down -- is the *largest* y.
        assert frame.to_kicad((0.0, 0.0)) == (100.0, 150.0)
        assert frame.to_kicad((0.0, 50.0)) == (100.0, 100.0)
        assert frame.to_kicad((80.0, 0.0)) == (180.0, 150.0)

    def test_conversion_round_trips(self) -> None:
        frame = frame_from_board(
            Board(outline=Outline(rect=(80.0, 50.0))), (100.0, 100.0)
        )
        for point in ((0.0, 0.0), (12.5, 37.25), (80.0, 50.0)):
            assert frame.to_source(frame.to_kicad(point)) == pytest.approx(point)

    @needs_kicad_libraries
    def test_an_asymmetric_outline_keeps_its_orientation(self, tmp_path: Path) -> None:
        """The notch is at the north in source, so at the *smallest* y in KiCad."""
        design = write_design(tmp_path, ASYMMETRIC)
        tree = board_of(design, tmp_path / "out")
        points = [
            (float(g.child("start").value(0)), float(g.child("start").value(1)))
            for g in edges(tree)
        ]
        assert points, "the outline was not emitted"
        top = min(y for _, y in points)
        bottom = max(y for _, y in points)
        # The notch cuts into the north edge, so the north edge has vertices at two
        # different y values and the south edge is a single straight run.
        north = sorted(x for x, y in points if abs(y - top) < 1e-6)
        south = sorted(x for x, y in points if abs(y - bottom) < 1e-6)
        assert len(north) == 4, f"expected the notched edge at the top, got {points}"
        assert len(south) == 2, f"expected the plain edge at the bottom, got {points}"

    @needs_kicad_libraries
    def test_a_fixed_part_lands_where_the_source_frame_says(
        self, tmp_path: Path
    ) -> None:
        design = write_design(tmp_path, ASYMMETRIC)
        tree = board_of(design, tmp_path / "out")
        x, y, _ = footprint_at(tree, "J1")
        # Source (6, 25) on a 30 mm-tall board, with the origin at (100, 100):
        # x is unchanged, y is measured down from the top.
        assert (x, y) == (106.0, 105.0)


# ---------------------------------------------------------------------------
# rings and canonical form
# ---------------------------------------------------------------------------


class TestRings:
    def test_a_rect_winds_counter_clockwise_in_the_source_frame(self) -> None:
        ring, _ = Board(outline=Outline(rect=(80.0, 50.0))).rings()
        assert ring_area(ring) > 0
        assert len(ring) == 4

    def test_a_corner_radius_becomes_four_arcs_and_four_lines(self) -> None:
        ring, _ = Board(
            outline=Outline(rect=(80.0, 50.0), corner_radius=2.0)
        ).rings()
        assert len(ring) == 8
        assert sum(1 for edge in ring if isinstance(edge, Line)) == 4

    def test_an_arc_bulges_outward(self) -> None:
        board = Board.model_validate(
            {
                "outline": {
                    "polygon": [
                        [0, 0], [80, 0],
                        {"arc_to": [85, 5], "center": [80, 5]},
                        [85, 50], [0, 50],
                    ]
                }
            }
        )
        ring, _ = board.rings()
        arc = ring[1]
        # The mid-point of a rounded outer corner is further from the centre of the
        # board than the chord between its ends.
        assert arc.mid[0] > 80.0 and arc.mid[1] < 5.0  # type: ignore[union-attr]
        assert ring_area(ring) > 80 * 50

    def test_canonical_form_is_independent_of_where_the_list_starts(self) -> None:
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        rings = []
        for shift in range(4):
            order = square[shift:] + square[:shift]
            rings.append(
                canonical_ring(
                    tuple(Line(order[i], order[(i + 1) % 4]) for i in range(4))
                )
            )
        assert all(ring == rings[0] for ring in rings)

    def test_canonical_form_is_independent_of_winding(self) -> None:
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        forward = tuple(Line(square[i], square[(i + 1) % 4]) for i in range(4))
        backward = tuple(edge.reversed() for edge in reversed(forward))
        assert canonical_ring(forward) == canonical_ring(backward)

    def test_a_slot_is_a_stadium(self) -> None:
        board = Board.model_validate(
            {
                "outline": {"rect": [40, 30]},
                "cutouts": [
                    {"slot": {"from": [10, 15], "to": [25, 15], "width": 2},
                     "reason": "cable"}
                ],
            }
        )
        _, cutouts = board.rings()
        points = tessellate(cutouts[0])
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        assert min(xs) == pytest.approx(9.0)
        assert max(xs) == pytest.approx(26.0)
        assert max(ys) - min(ys) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the placer against the real polygon
# ---------------------------------------------------------------------------


SHAPED = """
name: shaped
libraries: [../library/passives.yaml, ../library/connectors.yaml]
nets:
  A: {class: signal, description: one}
  B: {class: signal, description: two}
components:
  J1:
    part: CONN_BRK_1X04
    role: connector
    reason: Anchor.
    pins: {P1: A, P2: A, P3: B, P4: B}
  C1:
    part: C_10U_0805
    role: bulk
    for: J1
    reason: Follows its connector.
    pins: {"1": A, "2": B}
  C2:
    part: C_10U_0805
    role: bulk
    for: J1
    reason: And so does this one.
    pins: {"1": A, "2": B}
board:
  origin: bottom_left
  outline: {rect: [%(w)s, %(h)s]}
  cutouts:
%(cutouts)s
placement:
%(placement)s
layout:
  origin_mm: [100.0, 100.0]
  placement: {margin_mm: 1.0}
"""


def shaped(
    tmp_path: Path,
    *,
    width: float = 60,
    height: float = 40,
    cutouts: str = "    []\n",
    placement: str = "  J1:\n    fixed: {x: 8, y: 20}\n    reason: Anchor.\n",
) -> Path:
    body = SHAPED % {
        "w": width, "h": height, "cutouts": cutouts, "placement": placement
    }
    return write_design(tmp_path, body)


@needs_kicad_libraries
class TestPlacementAgainstTheOutline:
    def test_a_group_deforms_around_its_anchor(self, tmp_path: Path) -> None:
        """A relative intent may not move a fixed part. The group moves instead."""
        design = shaped(tmp_path)
        netlist = compile_netlist(design, Report())
        placement = plan_placement(
            netlist, extents=_extents(netlist), frame=frame_for(netlist)
        )
        anchor = placement.positions["J1"]
        assert (anchor.x, anchor.y) == (108.0, 120.0)
        # C1 and C2 are in J1's cluster through `for:`, so they pack beside it
        # rather than being shelf-packed into the corner of the board.
        for refdes in ("C1", "C2"):
            here = placement.positions[refdes]
            assert abs(here.x - anchor.x) < 25 and abs(here.y - anchor.y) < 25

    def test_nothing_is_packed_over_a_cutout(self, tmp_path: Path) -> None:
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.geometry import box as shapely_box

        design = shaped(
            tmp_path,
            cutouts=(
                "    - rect: [[20, 10], [46, 32]]\n"
                "      reason: A window that takes most of the middle.\n"
            ),
        )
        netlist = compile_netlist(design, Report())
        frame = frame_for(netlist)
        assert frame is not None
        extents = _extents(netlist)
        placement = plan_placement(netlist, extents=extents, frame=frame)
        hole = ShapelyPolygon(frame.cutout_polygons()[0])
        for refdes, placed in placement.positions.items():
            box = courtyard_box(extents[refdes], placed.x, placed.y, placed.rotation)
            assert not shapely_box(*box).intersects(hole), f"{refdes} is over the hole"

    def test_an_edge_constraint_sits_against_the_edge(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            placement=(
                "  J1:\n"
                "    edge: {side: north, offset_range: [20, 40]}\n"
                "    reason: Under the lid.\n"
            ),
        )
        netlist = compile_netlist(design, Report())
        frame = frame_for(netlist)
        assert frame is not None
        extents = _extents(netlist)
        placement = plan_placement(netlist, extents=extents, frame=frame)
        placed = placement.positions["J1"]
        box = courtyard_box(extents["J1"], placed.x, placed.y, placed.rotation)
        # North is the smallest KiCad y. The margin is 1 mm and the board's top edge
        # is at y = 100, so the courtyard's top sits on 101.
        assert box[1] == pytest.approx(101.0, abs=0.5)
        assert 20 <= frame.to_source((placed.x, placed.y))[0] <= 40


def _extents(netlist: object) -> dict:
    from aipcb.compile.place import component_extents

    extents, missing = component_extents(netlist)  # type: ignore[arg-type]
    assert not missing, missing
    return extents


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestMechanicalValidation:
    def test_two_fixed_courtyards_may_not_overlap(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            placement=(
                "  J1:\n    fixed: {x: 8, y: 20}\n    reason: Anchor.\n"
                "  C1:\n    fixed: {x: 8, y: 20}\n    reason: On top of it.\n"
            ),
        )
        assert "fixed-courtyards-overlap" in codes(report_for(design))

    def test_a_fixed_part_may_not_leave_the_board(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            placement="  J1:\n    fixed: {x: 59.5, y: 39}\n    reason: Off the edge.\n",
        )
        assert "fixed-part-outside-outline" in codes(report_for(design))

    def test_a_part_may_not_sit_over_a_cutout(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            cutouts=(
                "    - rect: [[4, 16], [20, 26]]\n"
                "      reason: A window right where the connector is pinned.\n"
            ),
        )
        assert "part-over-cutout" in codes(report_for(design))

    def test_a_region_swallowed_by_a_cutout_is_an_error(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            cutouts=(
                "    - rect: [[20, 10], [34, 24]]\n"
                "      reason: A window exactly where the LED was going.\n"
            ),
            placement=(
                "  J1:\n"
                "    region: {rect: [[23, 13], [31, 21]]}\n"
                "    reason: Under the light pipe, which is over the hole.\n"
            ),
        )
        assert "placement-set-empty" in codes(report_for(design))

    def test_an_edge_span_outside_the_board_is_an_error(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            placement=(
                "  J1:\n"
                "    edge: {side: north, offset_range: [70, 90]}\n"
                "    reason: Past the end of a 60 mm edge.\n"
            ),
        )
        assert "placement-set-empty" in codes(report_for(design))

    def test_a_cutout_may_not_leave_the_board(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            cutouts=(
                "    - rect: [[50, 30], [70, 50]]\n"
                "      reason: Past the corner.\n"
            ),
        )
        assert "cutout-outside-outline" in codes(report_for(design))

    def test_two_cutouts_may_not_overlap(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            cutouts=(
                "    - rect: [[20, 10], [30, 20]]\n      reason: One.\n"
                "    - rect: [[25, 15], [35, 25]]\n      reason: The other.\n"
            ),
        )
        assert "cutouts-overlap" in codes(report_for(design))

    def test_an_arc_must_be_an_arc(self, tmp_path: Path) -> None:
        body = SHAPED % {"w": 60, "h": 40, "cutouts": "    []\n",
                         "placement": "  J1:\n    fixed: {x: 8, y: 20}\n    reason: x\n"}
        body = body.replace(
            "  outline: {rect: [60, 40]}",
            "  outline:\n"
            "    polygon:\n"
            "      - [0, 0]\n"
            "      - [50, 0]\n"
            "      - {arc_to: [60, 10], center: [50, 4]}\n"
            "      - [60, 40]\n"
            "      - [0, 40]\n",
        )
        design = write_design(tmp_path, body)
        assert "board-arc-inconsistent" in codes(report_for(design))

    def test_a_self_intersecting_outline_is_an_error(self, tmp_path: Path) -> None:
        body = SHAPED % {"w": 60, "h": 40, "cutouts": "    []\n",
                         "placement": "  J1:\n    fixed: {x: 8, y: 20}\n    reason: x\n"}
        body = body.replace(
            "  outline: {rect: [60, 40]}",
            "  outline:\n"
            "    polygon:\n"
            "      - [0, 0]\n"
            "      - [60, 40]\n"
            "      - [60, 0]\n"
            "      - [0, 40]\n",
        )
        design = write_design(tmp_path, body)
        assert "board-outline-self-intersecting" in codes(report_for(design))

    def test_a_fixed_placement_wants_a_reason(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path, placement="  J1:\n    fixed: {x: 8, y: 20}\n"
        )
        assert "fixed-placement-without-reason" in codes(
            report_for(design), Severity.WARNING
        )

    def test_a_role_stands_in_for_a_reason(self, tmp_path: Path) -> None:
        design = shaped(
            tmp_path,
            placement="  J1:\n    fixed: {x: 8, y: 20}\n    role: mounting_hole\n",
        )
        assert "fixed-placement-without-reason" not in codes(report_for(design))

    def test_a_cutout_wants_a_reason(self, tmp_path: Path) -> None:
        design = shaped(tmp_path, cutouts="    - rect: [[20, 10], [30, 20]]\n")
        assert "cutout-without-reason" in codes(report_for(design), Severity.WARNING)

    def test_an_impossible_distance_is_a_conservative_warning(
        self, tmp_path: Path
    ) -> None:
        """Two parts pinned 40 mm apart cannot be within 2 mm of each other."""
        body = SHAPED % {
            "w": 60, "h": 40, "cutouts": "    []\n",
            "placement": (
                "  J1:\n    fixed: {x: 6, y: 20}\n    reason: West.\n"
                "  C1:\n    fixed: {x: 54, y: 20}\n    reason: East.\n"
            ),
        }
        body = body.replace(
            "board:\n",
            "constraints:\n"
            "  - kind: max_distance\n"
            "    between: [J1, C1]\n"
            "    mm: 2.0\n"
            "    reason: A loop area nobody can have here.\n"
            "board:\n",
            1,
        )
        design = write_design(tmp_path, body)
        report = report_for(design)
        assert "constraint-unreachable" in codes(report, Severity.WARNING)

    def test_a_reachable_distance_says_nothing(self, tmp_path: Path) -> None:
        body = SHAPED % {
            "w": 60, "h": 40, "cutouts": "    []\n",
            "placement": (
                "  J1:\n    fixed: {x: 20, y: 20}\n    reason: Here.\n"
                "  C1:\n    fixed: {x: 26, y: 20}\n    reason: Beside it.\n"
            ),
        }
        body = body.replace(
            "board:\n",
            "constraints:\n"
            "  - kind: max_distance\n"
            "    between: [J1, C1]\n"
            "    mm: 20.0\n"
            "    reason: Easy.\n"
            "board:\n",
            1,
        )
        design = write_design(tmp_path, body)
        assert "constraint-unreachable" not in codes(report_for(design))

    def test_placement_needs_a_board_block(self, tmp_path: Path) -> None:
        body = SHAPED % {"w": 60, "h": 40, "cutouts": "    []\n",
                         "placement": "  J1:\n    fixed: {x: 8, y: 20}\n    reason: x\n"}
        start = body.index("board:")
        end = body.index("placement:")
        design = write_design(tmp_path, body[:start] + body[end:])
        report = report_for(design)
        assert any("needs a `board:` block" in d.message for d in report)

    def test_declaring_the_outline_twice_is_an_error(self, tmp_path: Path) -> None:
        body = SHAPED % {"w": 60, "h": 40, "cutouts": "    []\n",
                         "placement": "  J1:\n    fixed: {x: 8, y: 20}\n    reason: x\n"}
        body = body.replace(
            "  placement: {margin_mm: 1.0}",
            "  placement: {margin_mm: 1.0}\n"
            "  outline: {shape: rect, width_mm: 60.0, height_mm: 40.0}",
        )
        design = write_design(tmp_path, body)
        report = report_for(design)
        assert any("declared twice" in d.message for d in report)

    def test_a_mechanical_block_naming_nothing_is_an_error(
        self, tmp_path: Path
    ) -> None:
        design = shaped(
            tmp_path,
            placement=(
                "  J1:\n    fixed: {x: 8, y: 20}\n    reason: x\n"
                "  U9:\n    fixed: {x: 30, y: 20}\n    reason: No such part.\n"
            ),
        )
        assert "unknown-mechanical-member" in codes(report_for(design))


# ---------------------------------------------------------------------------
# what reaches KiCad
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestEmission:
    def test_an_arc_is_emitted_as_an_arc(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "enclosure" / "design.yaml"
        tree = board_of(design, tmp_path)
        arcs = edges(tree, "gr_arc")
        assert len(arcs) == 1
        arc = arcs[0]
        assert arc.child("mid") is not None, "an arc without a mid point is a line"

    def test_a_cutout_is_emitted_as_a_closed_loop(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "enclosure" / "design.yaml"
        tree = board_of(design, tmp_path)
        lines = edges(tree)
        starts = [
            (float(g.child("start").value(0)), float(g.child("start").value(1)))
            for g in lines
        ]
        ends = [
            (float(g.child("end").value(0)), float(g.child("end").value(1)))
            for g in lines
        ]
        arcs = edges(tree, "gr_arc")
        for arc in arcs:
            starts.append((float(arc.child("start").value(0)), float(arc.child("start").value(1))))
            ends.append((float(arc.child("end").value(0)), float(arc.child("end").value(1))))
        assert sorted(starts) == sorted(ends), "the edge graphics do not close"
        # Seven outline edges plus four for the rectangular cutout.
        assert len(starts) == 11

    def test_every_courtyard_is_on_the_board(self, tmp_path: Path) -> None:
        """The acceptance claim for the shaped example, asserted rather than assumed."""
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.geometry import box as shapely_box

        design = REPO_ROOT / "examples" / "enclosure" / "design.yaml"
        netlist = compile_netlist(design, Report())
        frame = frame_for(netlist)
        assert frame is not None
        extents = _extents(netlist)
        placement = plan_placement(netlist, extents=extents, frame=frame)
        outline = ShapelyPolygon(frame.polygon())
        holes = [ShapelyPolygon(ring) for ring in frame.cutout_polygons()]
        assert holes, "the example is supposed to have a cutout"

        for refdes, placed in placement.positions.items():
            box = shapely_box(
                *courtyard_box(extents[refdes], placed.x, placed.y, placed.rotation)
            )
            assert outline.covers(box), f"{refdes} is not entirely on the board"
            for hole in holes:
                assert not box.intersects(hole), f"{refdes} covers a cutout"

    def test_the_router_reads_the_cutout_back_as_a_hole(self, tmp_path: Path) -> None:
        from aipcb.route.obstacles import board_rings

        design = REPO_ROOT / "examples" / "enclosure" / "design.yaml"
        tree = board_of(design, tmp_path)
        outline, holes = board_rings(tree)
        assert len(holes) == 1
        from shapely.geometry import Polygon as ShapelyPolygon

        board = ShapelyPolygon(outline, holes)
        # 48 x 34 less the cut corner and less the 6 x 8 window.
        assert board.area == pytest.approx(48 * 34 - 12 * 8 - 6 * 8 - (36 - 6 * 6 * 0.7854), abs=8)
        assert not ShapelyPolygon(outline).equals(board)

    def test_the_outline_is_not_its_convex_hull(self, tmp_path: Path) -> None:
        """The whole point of reading rings rather than hulling the points."""
        from shapely.geometry import Polygon as ShapelyPolygon

        from aipcb.route.obstacles import board_rings

        design = write_design(tmp_path, ASYMMETRIC)
        tree = board_of(design, tmp_path / "out")
        outline, _ = board_rings(tree)
        shape = ShapelyPolygon(outline)
        assert shape.area < shape.convex_hull.area, "the notch was filled in"

    def test_edge_clearance_reaches_the_project_file(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "enclosure" / "design.yaml"
        build_design(design, out_dir=tmp_path)
        project = json.loads(
            next(tmp_path.glob("*.kicad_pro")).read_text(encoding="utf-8")
        )
        rules = project["board"]["design_settings"]["rules"]
        assert rules["min_copper_edge_clearance"] == 0.3

    def test_a_comfortable_board_leaves_kicads_rules_alone(
        self, tmp_path: Path
    ) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        build_design(design, out_dir=tmp_path)
        project = json.loads(
            next(tmp_path.glob("*.kicad_pro")).read_text(encoding="utf-8")
        )
        # `rule_severities` is unconditional since M13.5 -- it is not a constraint
        # the design asked for, it is the set of rules KiCad would otherwise not
        # report at all. `rules` is the part a comfortable board leaves alone.
        assert "rules" not in project["board"]["design_settings"]

    def test_a_tight_net_class_relaxes_kicads_minimums(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
        build_design(design, out_dir=tmp_path)
        project = json.loads(
            next(tmp_path.glob("*.kicad_pro")).read_text(encoding="utf-8")
        )
        rules = project["board"]["design_settings"]["rules"]
        assert rules["min_via_diameter"] == 0.4
        assert rules["min_through_hole_diameter"] == 0.2


# ---------------------------------------------------------------------------
# fixed means fixed
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestFixedIsLaw:
    def test_a_hand_moved_fixed_part_goes_back(self, tmp_path: Path) -> None:
        design = shaped(tmp_path)
        out = tmp_path / "out"
        build_design(design, out_dir=out)
        board_path = next(out.glob("*.kicad_pcb"))
        tree = parse(board_path.read_text(encoding="utf-8"))
        for fp in tree.children("footprint"):
            if any(
                p.value(0) == "Reference" and p.value(1) == "J1"
                for p in fp.children("property")
            ):
                fp.replace("at", SNode("at").add(num(120), num(120), num(0)))
        board_path.write_text(dump(tree), encoding="utf-8")

        report = Report()
        build_design(design, out_dir=out, report=report)
        after = parse(board_path.read_text(encoding="utf-8"))
        assert footprint_at(after, "J1") == (108.0, 120.0, 0.0)
        assert "fixed-placement-drift" in codes(report, Severity.WARNING)

    def test_a_movable_part_keeps_its_hand_placement(self, tmp_path: Path) -> None:
        """M6's rule is unchanged for everything the source did not pin."""
        design = shaped(tmp_path)
        out = tmp_path / "out"
        build_design(design, out_dir=out)
        board_path = next(out.glob("*.kicad_pcb"))
        tree = parse(board_path.read_text(encoding="utf-8"))
        for fp in tree.children("footprint"):
            if any(
                p.value(0) == "Reference" and p.value(1) == "C1"
                for p in fp.children("property")
            ):
                fp.replace("at", SNode("at").add(num(133), num(133), num(0)))
        board_path.write_text(dump(tree), encoding="utf-8")

        build_design(design, out_dir=out)
        after = parse(board_path.read_text(encoding="utf-8"))
        assert footprint_at(after, "C1") == (133.0, 133.0, 0.0)


# ---------------------------------------------------------------------------
# sync-placement
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestSyncPlacement:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _workspace(self, tmp_path: Path) -> Path:
        shutil.copytree(REPO_ROOT / "examples" / "library", tmp_path / "library")
        folder = tmp_path / "enclosure"
        folder.mkdir()
        design = folder / "design.yaml"
        design.write_text(
            (REPO_ROOT / "examples" / "enclosure" / "design.yaml").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        return design

    def _nudge(self, design: Path, refdes: str, dx: float, dy: float) -> None:
        board_path = next(design.parent.glob("*.kicad_pcb"))
        tree = parse(board_path.read_text(encoding="utf-8"))
        x, y, rot = footprint_at(tree, refdes)
        for fp in tree.children("footprint"):
            if any(
                p.value(0) == "Reference" and p.value(1) == refdes
                for p in fp.children("property")
            ):
                fp.replace("at", SNode("at").add(num(x + dx), num(y + dy), num(rot)))
        board_path.write_text(dump(tree), encoding="utf-8")

    def test_a_clean_board_reports_no_drift(self, tmp_path: Path) -> None:
        design = self._workspace(tmp_path)
        self._run("build", str(design))
        result = self._run("sync-placement", str(design), "--json")
        assert json.loads(result.stdout)["drift"] == []

    def test_drift_is_reported_before_it_is_written(self, tmp_path: Path) -> None:
        design = self._workspace(tmp_path)
        self._run("build", str(design))
        self._nudge(design, "J1", 0.4, -0.2)
        before = design.read_text(encoding="utf-8")

        result = self._run("sync-placement", str(design), "--json")
        payload = json.loads(result.stdout)
        assert [d["refdes"] for d in payload["drift"]] == ["J1"]
        assert payload["drift"][0]["distance_mm"] == pytest.approx(0.447, abs=0.01)
        assert payload["written"] == []
        assert design.read_text(encoding="utf-8") == before, "nothing was asked for"

    def test_sync_round_trips_and_rebuilds_byte_identically(
        self, tmp_path: Path
    ) -> None:
        """Move in KiCad, sync, rebuild: the source now says where the part is.

        The board is not byte-identical to the *nudged* file -- the footprint's
        fingerprint records what the source said, and the source has just changed
        its mind -- but the position is the one that was nudged to, the drift is
        gone, and building again from the synced source is byte-identical.
        """
        design = self._workspace(tmp_path)
        self._run("build", str(design))
        self._nudge(design, "J1", 0.4, -0.2)
        board_path = next(design.parent.glob("*.kicad_pcb"))
        moved = footprint_at(parse(board_path.read_text(encoding="utf-8")), "J1")

        result = self._run("sync-placement", str(design), "--apply", "--yes")
        assert "wrote J1" in result.stdout, result.stdout + result.stderr

        self._run("build", str(design))
        synced = board_path.read_text(encoding="utf-8")
        assert footprint_at(parse(synced), "J1") == moved

        self._run("build", str(design))
        assert board_path.read_text(encoding="utf-8") == synced

        again = self._run("sync-placement", str(design), "--json")
        assert json.loads(again.stdout)["drift"] == []

    def test_the_edit_keeps_the_comments(self, tmp_path: Path) -> None:
        design = self._workspace(tmp_path)
        self._run("build", str(design))
        self._nudge(design, "J1", 0.4, -0.2)
        self._run("sync-placement", str(design), "--apply", "--yes")
        text = design.read_text(encoding="utf-8")
        assert "Datum B of mech/enclosure-v3.step" in text
        assert "fixed: { x: 4.4, y: 17.2, rot: 270 }" in text
        # Everything outside the entry is untouched.
        assert text.count("origin: bottom_left") == 1
        assert "# The board frame" in text


# ---------------------------------------------------------------------------
# fanout
# ---------------------------------------------------------------------------


def synthetic_board(pads: list[tuple[str, float, float, str]], size: float = 40.0) -> SNode:
    """A board with one footprint on it, built by hand.

    Small enough to reason about and free of any library, so a fanout test says
    something about the generator rather than about a particular part.
    """
    from aipcb.kicad.sexpr import quoted, sym

    root = SNode("kicad_pcb")
    corners = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    for index, start in enumerate(corners):
        end = corners[(index + 1) % 4]
        root.add(
            SNode("gr_line").add(
                SNode("start").add(num(start[0]), num(start[1])),
                SNode("end").add(num(end[0]), num(end[1])),
                SNode("layer").add(quoted("Edge.Cuts")),
                SNode("uuid").add(quoted(f"edge-{index}")),
            )
        )
    footprint = SNode("footprint").add(
        quoted("Test:BGA"),
        SNode("layer").add(quoted("F.Cu")),
        SNode("at").add(num(size / 2), num(size / 2), num(0)),
        SNode("property").add(quoted("Reference"), quoted("U1")),
    )
    for number, x, y, net in pads:
        pad = SNode("pad").add(
            quoted(number), sym("smd"), sym("circle"),
            SNode("at").add(num(x), num(y)),
            SNode("size").add(num(0.35), num(0.35)),
            SNode("layers").add(quoted("F.Cu")),
        )
        if net:
            pad.add(SNode("net").add(num(1), quoted(net)))
        footprint.add(pad)
    root.add(footprint)
    return root


class TestEscapeDirection:
    """Which way a pad leaves, in the package's own frame."""

    def test_a_ball_escapes_into_its_own_quadrant(self) -> None:
        from aipcb.route.fanout import escape_direction

        for local, expected in (
            ((1.0, 1.0), (1, 1)),
            ((-1.0, 1.0), (-1, 1)),
            ((1.0, -1.0), (1, -1)),
            ((-3.0, -0.5), (-1, -1)),
        ):
            direction = escape_direction(local, "dogbone")
            assert (direction[0] > 0) == (expected[0] > 0)
            assert (direction[1] > 0) == (expected[1] > 0)
            assert direction[0] ** 2 + direction[1] ** 2 == pytest.approx(1.0)

    def test_a_perimeter_pad_escapes_along_the_edge_it_sits_on(self) -> None:
        from aipcb.route.fanout import escape_direction

        assert escape_direction((-3.0, 0.4), "perimeter") == (-1.0, 0.0)
        assert escape_direction((0.4, 3.0), "perimeter") == (0.0, 1.0)
        assert escape_direction((3.0, -0.4), "perimeter") == (1.0, 0.0)
        assert escape_direction((-0.4, -3.0), "perimeter") == (0.0, -1.0)

    def test_via_count_follows_the_track_width(self) -> None:
        from aipcb.route.fanout import via_count

        assert via_count(0.25, 0.6) == 1
        assert via_count(1.2, 0.6) == 2
        assert via_count(10.0, 0.6) == 4, "capped: an escape is not a via farm"


class TestDogboneFanout:
    """A four-by-four array on a 1.27 mm pitch: four interior balls, twelve outside."""

    def _run(self, style: str = "auto") -> tuple:
        from aipcb.diagnostics import Report
        from aipcb.model.mech import Fanout
        from aipcb.netlist import Netlist
        from aipcb.route.fanout import generate_fanout
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.stack import stack_for

        pads = []
        number = 1
        for row in range(4):
            for col in range(4):
                pads.append(
                    (str(number), (col - 1.5) * 1.27, (row - 1.5) * 1.27, f"N{number}")
                )
                number += 1
        # A second pad numbered 5, as a shield tab or a split paddle would be: the
        # same number, a different piece of copper.
        pads.append(("5", 4.0, 4.0, "N5"))
        board = synthetic_board(pads)
        environment = extract_obstacles(board)
        netlist = Netlist(
            name="bga",
            revision="A",
            components={},
            nets={},
            fanout={"U1": Fanout(style=style, escape_layers=("B.Cu",))},  # type: ignore[arg-type]
        )
        # The generator asks the netlist which nets are real; here they all are.
        netlist.nets = dict.fromkeys(
            {net for _, _, _, net in pads if net}, None
        )  # type: ignore[arg-type]
        report = Report()
        result = generate_fanout(
            board, environment, netlist, stack_for(None), report
        )
        return result, environment, report

    def test_auto_picks_dogbone_for_an_area_array(self) -> None:
        """More than one interior pad is what an area array *is*."""
        from aipcb.route.fanout import choose_style

        centres = {
            f"U1.{i}": ((c - 1.5) * 1.27, (r - 1.5) * 1.27)
            for i, (r, c) in enumerate(
                ((r, c) for r in range(4) for c in range(4)), start=1
            )
        }
        assert choose_style(centres) == "dogbone"
        perimeter = {k: v for k, v in centres.items() if abs(v[0]) > 1 or abs(v[1]) > 1}
        assert choose_style(perimeter) == "perimeter"

    def test_every_escape_is_in_its_pad_s_quadrant(self) -> None:
        result, environment, _ = self._run()
        centre = (20.0, 20.0)
        for name, (_net, point, _layers) in result.terminals.items():
            pad = environment.pad_centres[name.removesuffix("@esc")]
            if abs(pad[0] - centre[0]) < 1e-9 and abs(pad[1] - centre[1]) < 1e-9:
                continue  # the middle ball has no quadrant
            for axis in (0, 1):
                if abs(pad[axis] - centre[axis]) < 1e-9:
                    continue
                assert (point[axis] - pad[axis]) * (pad[axis] - centre[axis]) > 0, (
                    f"{name} escaped toward the middle of the package"
                )

    def test_escapes_are_keyed_by_pad_instance_not_pad_number(self) -> None:
        result, environment, _ = self._run()
        # Two pads are numbered 5. Both are real copper, and both get their own
        # escape -- keyed by instance, which is what M7 learned the hard way.
        fives = sorted(n for n in result.terminals if n.startswith("U1.5"))
        assert fives == ["U1.5#2@esc", "U1.5@esc"]
        assert environment.pad_centres["U1.5"] != environment.pad_centres["U1.5#2"]

    def test_the_package_pads_stop_being_terminals(self) -> None:
        result, environment, _ = self._run()
        result.apply(environment)
        assert not [k for k in environment.pad_nets if not k.endswith("@esc")]
        assert all(
            environment.pad_layers[name] == frozenset({"B.Cu"})
            for name in result.terminals
        )


@needs_kicad_libraries
class TestFanoutOnARealPackage:
    def test_the_qfn_escapes_every_connected_pad(self, tmp_path: Path) -> None:
        from aipcb.diagnostics import Report
        from aipcb.route.fanout import generate_fanout
        from aipcb.route.geometry import edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.stack import stack_for

        design = REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
        netlist = compile_netlist(design, Report())
        tree = board_of(design, tmp_path)
        environment = extract_obstacles(
            tree, edge_clearance=edge_clearance_for(netlist)
        )
        report = Report()
        result = generate_fanout(
            tree, environment, netlist, stack_for(netlist.layout), report
        )
        connected = {
            pad
            for pad, net in environment.pad_nets.items()
            if pad.startswith("U1.") and net in netlist.nets
        }
        assert len(connected) == 16
        assert result.replaced == connected, "a connected pad was left unescaped"
        assert not [d for d in report if d.severity is Severity.WARNING]

    def test_an_unused_pad_gets_no_fanout(self, tmp_path: Path) -> None:
        from aipcb.diagnostics import Report
        from aipcb.route.fanout import generate_fanout
        from aipcb.route.geometry import edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.stack import stack_for

        design = REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
        netlist = compile_netlist(design, Report())
        tree = board_of(design, tmp_path)
        environment = extract_obstacles(
            tree, edge_clearance=edge_clearance_for(netlist)
        )
        result = generate_fanout(
            tree, environment, netlist, stack_for(netlist.layout), Report()
        )
        # Pin 1 is a GPIO the design never connects.
        assert "U1.1@esc" not in result.terminals

    def test_the_exposed_pad_gets_a_via_through_it(self, tmp_path: Path) -> None:
        from aipcb.diagnostics import Report
        from aipcb.route.fanout import generate_fanout
        from aipcb.route.geometry import edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.stack import stack_for

        design = REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
        netlist = compile_netlist(design, Report())
        tree = board_of(design, tmp_path)
        environment = extract_obstacles(
            tree, edge_clearance=edge_clearance_for(netlist)
        )
        result = generate_fanout(
            tree, environment, netlist, stack_for(netlist.layout), Report()
        )
        pad = environment.pad_centres["U1.33"]
        _net, point, _layers = result.terminals["U1.33@esc"]
        assert point == pytest.approx(pad), "the thermal pad's via is not in it"


# ---------------------------------------------------------------------------
# a hole in the board is a hole in the free space
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestRoutingAroundACutout:
    """A cutout separates paths, and the topology model already knows what to do."""

    def _routed(self, tmp_path: Path) -> tuple:
        from aipcb.route.geometry import edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.plan import route_board

        design = shaped(
            tmp_path,
            width=60,
            height=40,
            cutouts=(
                "    - rect: [[24, 6], [36, 34]]\n"
                "      reason: A slot across the middle, so a wire has to choose a "
                "side.\n"
            ),
            placement=(
                "  J1:\n    fixed: {x: 8, y: 20}\n    reason: West of the slot.\n"
                "  C1:\n    fixed: {x: 52, y: 20}\n    reason: East of it.\n"
                "  C2:\n    fixed: {x: 52, y: 30}\n    reason: Out of the way.\n"
            ),
        )
        out = tmp_path / "out"
        build_design(design, out_dir=out)
        tree = parse(next(out.glob("*.kicad_pcb")).read_text(encoding="utf-8"))
        netlist = compile_netlist(design, Report())
        environment = extract_obstacles(
            tree, edge_clearance=edge_clearance_for(netlist)
        )
        routed = route_board(tree, netlist, Report())
        return routed, environment

    def test_no_copper_crosses_the_hole(self, tmp_path: Path) -> None:
        from shapely.geometry import LineString
        from shapely.geometry import Polygon as ShapelyPolygon

        routed, environment = self._routed(tmp_path)
        assert routed.connections, "nothing was routed"
        hole = ShapelyPolygon(environment.cutouts[0])
        for connection in routed.connections:
            for leg in connection.legs:
                if len(leg.points) < 2:
                    continue
                assert not LineString(leg.points).intersects(hole), (
                    f"{leg.net} runs through the slot"
                )

    def test_the_straight_line_would_have(self, tmp_path: Path) -> None:
        """Proof the test board is the shape the test thinks it is."""
        from shapely.geometry import LineString
        from shapely.geometry import Polygon as ShapelyPolygon

        _routed, environment = self._routed(tmp_path)
        hole = ShapelyPolygon(environment.cutouts[0])
        west = environment.pad_centres["J1.1"]
        east = environment.pad_centres["C1.1"]
        assert LineString([west, east]).intersects(hole)

    def test_the_two_ways_round_are_different_homotopy_classes(
        self, tmp_path: Path
    ) -> None:
        """Which side of the hole a route takes is a fact the model can express."""
        from shapely.geometry import LineString

        from aipcb.route.triangulate import free_space, triangulate_free

        _routed, environment = self._routed(tmp_path)
        free = free_space(environment, [], edge_margin=0.5)
        triangulation = triangulate_free(free)

        def crossed(points: list[tuple[float, float]]) -> frozenset[int]:
            line = LineString(points)
            return frozenset(
                index
                for index, diagonal in enumerate(triangulation.diagonals)
                if line.crosses(LineString((diagonal.a, diagonal.b)))
            )

        west = environment.pad_centres["J1.1"]
        east = environment.pad_centres["C1.1"]
        # KiCad's y points down, so the smaller y is the north side of the slot.
        north = crossed([west, (west[0], 103.0), (east[0], 103.0), east])
        south = crossed([west, (west[0], 137.0), (east[0], 137.0), east])
        assert north and south
        assert north != south, "the two ways round the hole look the same"


# ---------------------------------------------------------------------------
# handing over
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestHandOver:
    def _route(self, tmp_path: Path) -> tuple:
        from aipcb.route.plan import route_board

        design = REPO_ROOT / "examples" / "overconstrained" / "design.yaml"
        result = build_design(design, out_dir=tmp_path)
        board_path = next(tmp_path.glob("*.kicad_pcb"))
        tree = parse(board_path.read_text(encoding="utf-8"))
        report = Report()
        routed = route_board(
            tree,
            result.netlist,
            report,
            topologies=tuple(result.netlist.layout.routes)
            if result.netlist.layout
            else (),
        )
        return routed, report, tree, result

    def test_the_router_refuses_rather_than_squeezing(self, tmp_path: Path) -> None:
        routed, _report, _tree, _result = self._route(tmp_path)
        assert routed.failed, "this board is supposed to be impossible"
        assert routed.connections, "and the rest of it is supposed to be routed"

    def test_the_report_is_machine_readable(self, tmp_path: Path) -> None:
        routed, _report, _tree, _result = self._route(tmp_path)
        handed = routed.handed_over()
        assert handed
        from aipcb.route.plan import HANDOVER_KINDS

        for entry in handed:
            assert entry["unrouted"] in HANDOVER_KINDS
            assert entry["net"] and entry["from"] and entry["to"]
            assert entry["reason"]
        blocked = [e for e in handed if e["unrouted"] == "over_complexity"]
        assert blocked, "capacity is what this board runs out of"
        cut = blocked[0]["blocked_at"][0]
        assert cut["layer"] == "F.Cu"
        assert len(cut["at"]) == 2
        assert cut["width_mm"] > 0 and cut["demand_mm"] > 0
        assert cut["nets"], "the report has to name who else wanted the corridor"

    def test_the_warning_names_the_category(self, tmp_path: Path) -> None:
        _routed, report, _tree, _result = self._route(tmp_path)
        handed = [d for d in report if d.code == "route-handed-over"]
        assert handed
        assert any("unrouted:" in d.message for d in handed)
        assert all(d.context.get("unrouted") for d in handed)

    def test_check_json_lists_what_was_handed_over(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable, "-m", "aipcb.cli", "check",
                str(REPO_ROOT / "examples" / "overconstrained" / "design.yaml"),
                "--json", "--no-erc", "--no-drc",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(result.stdout)
        handed = payload["summary"]["routing"]["handed_over"]
        assert handed, result.stdout[:400]
        from aipcb.route.plan import HANDOVER_KINDS

        assert {e["unrouted"] for e in handed} <= set(HANDOVER_KINDS)

    def test_a_hand_routed_net_survives_the_next_build(self, tmp_path: Path) -> None:
        """What the router hands over, a human finishes -- and M6 keeps it."""
        from aipcb.kicad.sexpr import quoted, sym

        shutil.copytree(REPO_ROOT / "examples" / "library", tmp_path / "library")
        folder = tmp_path / "overconstrained"
        folder.mkdir()
        design = folder / "design.yaml"
        design.write_text(
            (
                REPO_ROOT / "examples" / "overconstrained" / "design.yaml"
            ).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", "all", str(design)],
            capture_output=True, text=True, check=False,
        )
        board_path = next(folder.glob("*.kicad_pcb"))
        tree = parse(board_path.read_text(encoding="utf-8"))

        # Route one of the handed-over nets by hand, as a person would in KiCad.
        code = next(n.value(0) for n in tree.children("net") if n.value(1) == "SWAP_D")
        hand = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        tree.add(
            SNode("segment").add(
                SNode("start").add(num(104), num(112)),
                SNode("end").add(num(120), num(112)),
                SNode("width").add(num(0.25)),
                SNode("layer").add(quoted("B.Cu")),
                SNode("net").add(sym(str(code))),
                SNode("uuid").add(quoted(hand)),
            )
        )
        board_path.write_text(dump(tree), encoding="utf-8")

        subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "build", str(design)],
            capture_output=True, text=True, check=False,
        )
        after = parse(board_path.read_text(encoding="utf-8"))
        assert any(s.get("uuid") == hand for s in after.children("segment")), (
            "the hand-routed net did not survive the rebuild"
        )

        # And a later routing run treats it as law rather than routing through it.
        run = subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", "all", str(design)],
            capture_output=True, text=True, check=False,
        )
        assert "routing-around-manual-copper" in run.stdout, run.stdout[-800:]
        again = parse(board_path.read_text(encoding="utf-8"))
        assert any(s.get("uuid") == hand for s in again.children("segment"))


# ---------------------------------------------------------------------------
# the migration promise
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestMigration:
    """A `board:` rect must emit exactly what the old `layout.outline` emitted."""

    BODY = """
name: migrate
libraries: [../library/connectors.yaml]
nets:
  A: {class: signal, description: one}
  B: {class: signal, description: two}
components:
  J1:
    part: CONN_BRK_1X04
    role: connector
    reason: Something to place.
    pins: {P1: A, P2: A, P3: B, P4: B}
%(block)s
"""

    def _edges(self, tmp_path: Path, block: str, name: str) -> list[tuple]:
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "library").symlink_to(LIBRARY, target_is_directory=True)
        folder = root / "design"
        folder.mkdir()
        design = folder / "design.yaml"
        design.write_text(self.BODY % {"block": block}, encoding="utf-8")
        tree = board_of(design, folder / "out")
        return [
            (
                g.name,
                g.child("start").value(0), g.child("start").value(1),
                g.child("end").value(0), g.child("end").value(1),
                g.get("uuid"),
            )
            for g in tree.children()
            if g.name in ("gr_line", "gr_arc") and g.get("layer") == "Edge.Cuts"
        ]

    def test_a_rect_block_emits_the_same_edge_as_the_old_outline(
        self, tmp_path: Path
    ) -> None:
        old = self._edges(
            tmp_path,
            "layout:\n"
            "  outline: {shape: rect, width_mm: 40.0, height_mm: 30.0}\n"
            "  origin_mm: [100.0, 100.0]\n",
            "old",
        )
        new = self._edges(
            tmp_path,
            "board:\n  origin: bottom_left\n  outline: {rect: [40.0, 30.0]}\n"
            "layout:\n  origin_mm: [100.0, 100.0]\n",
            "new",
        )
        assert old == new, "migrating to `board:` changed the emitted edge"
        assert len(old) == 4


@needs_kicad_libraries
class TestFanoutStyles:
    def _fanout(self, tmp_path: Path, block: str) -> tuple:
        from aipcb.route.fanout import generate_fanout
        from aipcb.route.geometry import edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.stack import stack_for

        source = (
            REPO_ROOT / "examples" / "qfn-fanout" / "design.yaml"
        ).read_text(encoding="utf-8")
        start = source.index("\nfanout:\n") + 1
        end = source.index("\nboard:\n") + 1
        shutil.copytree(REPO_ROOT / "examples" / "library", tmp_path / "library")
        folder = tmp_path / "qfn"
        folder.mkdir()
        design = folder / "design.yaml"
        design.write_text(source[:start] + block + source[end:], encoding="utf-8")

        netlist = compile_netlist(design, Report())
        tree = board_of(design, folder / "out")
        environment = extract_obstacles(
            tree, edge_clearance=edge_clearance_for(netlist)
        )
        report = Report()
        result = generate_fanout(
            tree, environment, netlist, stack_for(netlist.layout), report
        )
        return result, report

    def test_style_none_lays_nothing(self, tmp_path: Path) -> None:
        result, _report = self._fanout(
            tmp_path, "fanout:\n  U1:\n    style: none\n\n"
        )
        assert not result.terminals
        assert not result.connections

    def test_via_in_pad_warns_about_what_it_costs(self, tmp_path: Path) -> None:
        _result, report = self._fanout(
            tmp_path,
            "fanout:\n  U1:\n    style: via_in_pad\n    escape_layers: [B.Cu]\n"
            "    via: {drill: 0.2, diameter: 0.4}\n\n",
        )
        assert "fanout-via-in-pad" in codes(report, Severity.WARNING)

    def test_an_unknown_escape_layer_is_an_error(self, tmp_path: Path) -> None:
        _result, report = self._fanout(
            tmp_path,
            "fanout:\n  U1:\n    style: auto\n    escape_layers: [In1.Cu]\n\n",
        )
        assert "fanout-unknown-layer" in codes(report, Severity.ERROR)

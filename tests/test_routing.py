"""Topological routing: the model, the geometry, and what KiCad makes of the result.

The acceptance bar is the same as every other milestone's: generated copper passes
``kicad-cli pcb drc``, and the output is byte-stable. The unit tests below exist
because several of the bugs found while building this were invisible at that level
-- a route with an inverted portal orientation is still legal, still terminates, and
is simply seven times longer than it should be.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

from aipcb.compile.build import build_design
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import dump, parse
from aipcb.route.emit import attach_tracks, track_uuid
from aipcb.route.funnel import orient_portals, signed_area, tighten
from aipcb.route.model import Pass, RouteTopology, ViaHop, parse_node
from aipcb.route.obstacles import convex_hull, extract_obstacles, inflate
from aipcb.route.plan import route_board, spanning_routes
from aipcb.route.stretch import RouteRules, StretchError, prepare, side_point, stretch_route
from aipcb.route.triangulate import build_triangulation, reduce_crossings

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

EXAMPLES = ("led-blinker", "ldo-supply", "usb-port", "routing-demo", "diff-pair")


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------


class TestTopologyModel:
    def test_parses_a_sketch(self) -> None:
        route = RouteTopology.model_validate(
            {
                "net": "USB_DP",
                "from": "J1.3",
                "to": "R1.1",
                "passes": [
                    {"kind": "pass", "obstacle": "J1.4", "side": "left"},
                    {"kind": "via", "to_layer": "B.Cu", "name": "v1"},
                ],
            }
        )
        assert route.key() == "USB_DP/J1.3>R1.1"
        assert isinstance(route.passes[0], Pass)
        assert isinstance(route.passes[1], ViaHop)
        assert route.layers_used() == ("F.Cu", "B.Cu")

    def test_endpoints_must_be_pads(self) -> None:
        with pytest.raises(ValueError, match="pad reference"):
            RouteTopology.model_validate({"net": "N", "from": "U1", "to": "R1.1"})

    def test_a_route_cannot_end_where_it_starts(self) -> None:
        with pytest.raises(ValueError, match="same pad"):
            RouteTopology.model_validate({"net": "N", "from": "U1.1", "to": "U1.1"})

    def test_side_is_constrained(self) -> None:
        with pytest.raises(ValueError):
            RouteTopology.model_validate(
                {
                    "net": "N", "from": "U1.1", "to": "R1.1",
                    "passes": [{"kind": "pass", "obstacle": "C1", "side": "north"}],
                }
            )

    @pytest.mark.parametrize(
        ("reference", "expected"),
        [("U1.7", ("U1", "7")), ("U1", ("U1", None)), ("via:v1", ("via:v1", None))],
    )
    def test_node_parsing(self, reference: str, expected: tuple[str, str | None]) -> None:
        assert parse_node(reference) == expected

    def test_topologies_reach_the_layout_model(self) -> None:
        from aipcb.model.layout import Layout

        layout = Layout.model_validate(
            {"routes": [{"net": "N", "from": "U1.1", "to": "R1.1"}]}
        )
        assert layout.routes[0].net == "N"


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


class TestGeometry:
    def test_inflation_never_shrinks(self) -> None:
        square = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0))
        grown = inflate(square, 0.5)
        xs = [p[0] for p in grown]
        ys = [p[1] for p in grown]
        assert min(xs) <= -0.5 and max(xs) >= 2.5
        assert min(ys) <= -0.5 and max(ys) >= 2.5

    def test_inflation_is_conservative(self) -> None:
        """An under-stated obstacle would produce a clearance violation."""
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        grown = inflate(square, 0.3)
        for x, y in ((0.0, 0.0), (1.0, 1.0)):
            nearest = min(math.dist((x, y), p) for p in grown)
            assert nearest >= 0.3 - 1e-9

    def test_convex_hull_of_collinear_points(self) -> None:
        assert len(convex_hull(((0.0, 0.0), (1.0, 1.0), (2.0, 2.0)))) <= 3

    def test_side_point_is_left_of_travel(self) -> None:
        """Board coordinates are Y-down, so left of east is *north*."""
        from aipcb.route.obstacles import Obstacle

        obstacle = Obstacle(
            "x", ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
        )
        left = side_point(obstacle, (1.0, 0.0), "left")
        right = side_point(obstacle, (1.0, 0.0), "right")
        assert left[1] < 0, "left of eastward travel should be above (smaller y)"
        assert right[1] > 0

    def test_side_point_follows_the_direction(self) -> None:
        from aipcb.route.obstacles import Obstacle

        obstacle = Obstacle("x", ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)))
        east = side_point(obstacle, (1.0, 0.0), "left")
        west = side_point(obstacle, (-1.0, 0.0), "left")
        assert east[1] * west[1] < 0, "reversing travel should swap which side is left"


class TestReduction:
    def test_cancels_adjacent_repeats(self) -> None:
        assert reduce_crossings([1, 2, 3, 3, 4]) == [1, 2, 4]

    def test_cancels_repeatedly(self) -> None:
        assert reduce_crossings([1, 2, 2, 1, 3]) == [3]

    def test_leaves_a_simple_sequence_alone(self) -> None:
        assert reduce_crossings([1, 2, 3]) == [1, 2, 3]

    def test_empty(self) -> None:
        assert reduce_crossings([]) == []


class TestFunnel:
    def test_goes_straight_when_it_can(self) -> None:
        portals = [((2.0, 0.0), (2.0, 4.0)), ((4.0, 0.0), (4.0, 4.0))]
        assert tighten((0.0, 2.0), (6.0, 2.0), portals) == [(0.0, 2.0), (6.0, 2.0)]

    def test_bends_around_a_notch(self) -> None:
        portals = [
            ((1.0, 0.0), (1.0, 10.0)),
            ((3.0, 6.0), (3.0, 10.0)),
            ((5.0, 0.0), (5.0, 10.0)),
        ]
        path = tighten((0.0, 5.0), (6.0, 5.0), portals)
        assert (3.0, 6.0) in path, "the path must go round the notch"

    def test_no_portals_is_a_straight_line(self) -> None:
        assert tighten((0.0, 0.0), (5.0, 5.0), []) == [(0.0, 0.0), (5.0, 5.0)]

    def test_collinear_points_are_dropped(self) -> None:
        from aipcb.route.funnel import _dedupe

        assert _dedupe([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]) == [(0.0, 0.0), (2.0, 0.0)]

    def test_portal_orientation_uses_the_y_down_sign(self) -> None:
        """The inverted sign is legal, terminates, and gives a 7x longer route."""
        diagonals = [((1.0, 0.0), (1.0, 4.0))]
        waypoints = [(0.0, 2.0), (2.0, 2.0)]
        left, right = orient_portals(diagonals, waypoints)[0]
        assert signed_area(waypoints[0], waypoints[1], left) < 0
        assert signed_area(waypoints[0], waypoints[1], right) > 0


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


class TestSpanningRoutes:
    def test_connects_every_pad_once(self) -> None:
        centres = {"A.1": (0.0, 0.0), "B.1": (1.0, 0.0), "C.1": (2.0, 0.0)}
        routes = spanning_routes("N", list(centres), centres, "F.Cu")
        assert len(routes) == 2
        touched = {r.from_ for r in routes} | {r.to for r in routes}
        assert touched == set(centres)

    def test_prefers_short_connections(self) -> None:
        """A star from one pad would be simpler and much longer."""
        centres = {"A.1": (0.0, 0.0), "B.1": (1.0, 0.0), "C.1": (10.0, 0.0)}
        routes = spanning_routes("N", list(centres), centres, "F.Cu")
        pairs = {tuple(sorted((r.from_, r.to))) for r in routes}
        assert ("A.1", "B.1") in pairs
        assert ("A.1", "C.1") not in pairs

    def test_a_single_pad_needs_no_route(self) -> None:
        assert spanning_routes("N", ["A.1"], {"A.1": (0.0, 0.0)}, "F.Cu") == []

    def test_is_deterministic(self) -> None:
        centres = {f"P{i}.1": (float(i % 4), float(i // 4)) for i in range(9)}
        first = spanning_routes("N", list(centres), centres, "F.Cu")
        second = spanning_routes("N", sorted(centres, reverse=True), centres, "F.Cu")
        assert [r.key() for r in first] == [r.key() for r in second]


# ---------------------------------------------------------------------------
# against a real board
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestOnRealBoards:
    def _board(self, name: str, tmp_path: Path):
        result = build_design(
            REPO_ROOT / "examples" / name / "design.yaml", out_dir=tmp_path
        )
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        return result, board_path, parse(board_path.read_text(encoding="utf-8"))

    def test_obstacles_come_out_of_the_board(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        assert len(environment.outline) >= 3
        assert environment.pad_centres
        assert environment.pad_nets["J1.1"] == "VBUS"

    def test_pads_sharing_a_number_are_all_kept(self, tmp_path: Path) -> None:
        """The micro-B's shield tabs are all pad 6; losing three of them shorted a net."""
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        shields = [k for k in environment.obstacles if k.startswith("J1.6")]
        assert len(shields) > 1, shields

    def test_a_nets_own_pads_do_not_block_it(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        blocking = {o.name for o in environment.blocking("VBUS", "F.Cu")}
        assert not any(name.startswith("J1.1") for name in blocking)

    def test_component_bodies_do_not_block_copper(self, tmp_path: Path) -> None:
        """A courtyard says how much room a part needs, not that copper is banned."""
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        blocking = {o.kind for o in environment.blocking("VBUS", "F.Cu")}
        assert "body" not in blocking

    def test_triangulation_covers_the_free_area(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        triangulation = build_triangulation(
            environment, environment.blocking("USB_DP", "F.Cu")
        )
        assert len(triangulation.triangles) > 10
        assert len(triangulation.diagonals) > 10

    def test_a_route_is_near_the_straight_line(self, tmp_path: Path) -> None:
        """Rubber-band tightening should barely lengthen an unobstructed run."""
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        environment, triangulation = prepare(board, "USB_DP", "F.Cu", rules)
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "R1.1"}
        )
        result = stretch_route(route, environment, triangulation, rules)
        direct = math.dist(
            environment.pad_centres[environment.resolve_pad("J1.3") or ""],
            environment.pad_centres[environment.resolve_pad("R1.1") or ""],
        )
        assert result.length < direct * 1.25, (
            f"{result.length:.1f} mm against a {direct:.1f} mm straight line"
        )

    def test_stretching_is_deterministic(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "R1.1"}
        )
        runs = []
        for _ in range(2):
            environment, triangulation = prepare(board, "USB_DP", "F.Cu", rules)
            runs.append(stretch_route(route, environment, triangulation, rules).points)
        assert runs[0] == runs[1]

    def test_an_unreachable_endpoint_is_reported(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        environment, triangulation = prepare(board, "USB_DP", "F.Cu", rules)
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "Q9.1"}
        )
        with pytest.raises(StretchError, match="not a pad"):
            stretch_route(route, environment, triangulation, rules)

    def test_via_hops_are_refused_clearly(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        environment, triangulation = prepare(board, "USB_DP", "F.Cu", rules)
        route = RouteTopology.model_validate(
            {
                "net": "USB_DP", "from": "J1.3", "to": "R1.1",
                "passes": [{"kind": "via", "to_layer": "B.Cu"}],
            }
        )
        with pytest.raises(StretchError, match="does not do yet"):
            stretch_route(route, environment, triangulation, rules)


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


@needs_kicad_libraries
@needs_kicad_cli
class TestRoutedBoardsPassDrc:
    def _route(self, name: str, tmp_path: Path):
        from aipcb.kicad.cli import run_kicad

        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / name / "design.yaml",
            out_dir=tmp_path,
            report=report,
        )
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        board = parse(board_path.read_text(encoding="utf-8"))
        routed = route_board(board, result.netlist, report)
        attach_tracks(board, routed.with_endpoints(), sorted(result.netlist.nets))
        board_path.write_text(dump(board), encoding="utf-8")

        report_path = tmp_path / "drc.json"
        run = run_kicad(
            "pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
            "-o", str(report_path), str(board_path),
        )
        assert run.returncode == 0, f"KiCad rejected the board: {run.stdout}{run.stderr}"
        return routed, json.loads(report_path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("name", EXAMPLES)
    def test_routed_board_has_no_drc_violations(self, name: str, tmp_path: Path) -> None:
        _, drc = self._route(name, tmp_path)
        violations = [
            f"[{v['severity']}] {v['type']}: {v['description']}"
            for v in drc["violations"]
        ]
        assert not violations, "\n".join(violations)

    @pytest.mark.parametrize("name", EXAMPLES)
    def test_routing_keeps_schematic_parity(self, name: str, tmp_path: Path) -> None:
        _, drc = self._route(name, tmp_path)
        assert not drc["schematic_parity"]

    @pytest.mark.parametrize("name", EXAMPLES)
    def test_something_actually_got_routed(self, name: str, tmp_path: Path) -> None:
        routed, _ = self._route(name, tmp_path)
        assert routed.routed
        assert routed.total_length > 0

    def test_a_simple_board_routes_completely(self, tmp_path: Path) -> None:
        """The end-to-end claim: source in, a finished board out."""
        routed, drc = self._route("ldo-supply", tmp_path)
        assert not routed.failed, [why for _, why in routed.failed]
        assert not drc["unconnected_items"], "every connection should be copper"
        assert not drc["violations"]

    def test_routing_is_byte_stable(self, tmp_path: Path) -> None:
        boards = []
        for run in ("a", "b"):
            target = tmp_path / run
            report = Report()
            result = build_design(
                REPO_ROOT / "examples" / "ldo-supply" / "design.yaml",
                out_dir=target,
                report=report,
            )
            path = next(p for p in result.written if p.suffix == ".kicad_pcb")
            board = parse(path.read_text(encoding="utf-8"))
            routed = route_board(board, result.netlist, report)
            attach_tracks(board, routed.with_endpoints(), sorted(result.netlist.nets))
            boards.append(dump(board))
        assert boards[0] == boards[1]

    def test_track_uuids_are_derived_not_random(self) -> None:
        assert track_uuid("GND", "U1.1", "C1.2", 0) == track_uuid("GND", "U1.1", "C1.2", 0)
        assert track_uuid("GND", "U1.1", "C1.2", 0) != track_uuid("GND", "U1.1", "C1.2", 1)


@needs_kicad_libraries
@needs_kicad_cli
class TestRouteCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", *args],
            capture_output=True, text=True, check=False,
        )

    def test_check_reports_nothing_to_check(self) -> None:
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        result = self._run("check", str(design))
        assert result.returncode == 0, result.stderr
        assert "no route topologies" in result.stdout

    def test_check_validates_declared_topologies(self) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        payload = json.loads(self._run("check", str(design), "--json").stdout)
        assert payload["routes"]["checked"] >= 1

    def test_route_all_writes_tracks(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        result = self._run("all", str(design), "--out", str(tmp_path), "--json")
        payload = json.loads(result.stdout)
        assert payload["routing"]["routed"] > 0
        assert payload["routing"]["segments"] > 0
        board = parse((tmp_path / "ldo-supply.kicad_pcb").read_text(encoding="utf-8"))
        assert list(board.children("segment"))


@needs_kicad_libraries
class TestCongestion:
    """Auto-topology weighs corridor width, not just length."""

    def _route(self, name: str, tmp_path: Path, congestion: float):
        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / name / "design.yaml",
            out_dir=tmp_path / f"c{congestion}",
            report=report,
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        topologies = (
            tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        )
        return route_board(
            board, result.netlist, report,
            topologies=topologies, congestion=congestion,
        )

    def test_avoiding_narrow_gaps_routes_more(self, tmp_path: Path) -> None:
        """The measurement behind the default: shortest-only strands a connection."""
        greedy = self._route("led-blinker", tmp_path, 0.0)
        careful = self._route("led-blinker", tmp_path, 1.0)
        assert len(careful.routed) > len(greedy.routed)

    def test_and_uses_no_more_copper(self, tmp_path: Path) -> None:
        greedy = self._route("led-blinker", tmp_path, 0.0)
        careful = self._route("led-blinker", tmp_path, 1.0)
        assert careful.total_length <= greedy.total_length

    def test_congestion_is_deterministic(self, tmp_path: Path) -> None:
        first = self._route("usb-port", tmp_path / "a", 1.0)
        second = self._route("usb-port", tmp_path / "b", 1.0)
        assert [r.points for r in first.routed] == [r.points for r in second.routed]

    def test_gate_width_measures_the_corridor(self, tmp_path: Path) -> None:
        from aipcb.route.obstacles import extract_obstacles

        result = build_design(
            REPO_ROOT / "examples" / "usb-port" / "design.yaml", out_dir=tmp_path
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        environment = extract_obstacles(board)
        triangulation = build_triangulation(
            environment, environment.blocking("USB_DP", "F.Cu")
        )
        widths = [triangulation.gate_width(i) for i in range(len(triangulation.diagonals))]
        assert all(w > 0 for w in widths)
        assert max(widths) > min(widths), "a board has both wide and narrow corridors"


class TestImpedanceEstimate:
    """Deriving a pair's gap from its impedance target."""

    def test_a_reachable_target_gives_a_gap(self) -> None:
        from aipcb.route.diffpair import estimate_gap

        gap = estimate_gap(100.0, width=0.2, height=0.2)
        assert 0.0 < gap < 1.0

    def test_a_wider_gap_means_higher_impedance(self) -> None:
        from aipcb.route.diffpair import estimate_gap

        assert estimate_gap(110.0, 0.2, 0.2) > estimate_gap(90.0, 0.2, 0.2)

    def test_an_unreachable_target_is_refused_not_clamped(self) -> None:
        """Clamping would hide that the trace width, not the gap, is wrong."""
        from aipcb.route.diffpair import ImpedanceUnreachable, achievable_range, estimate_gap

        low, _high = achievable_range(0.34, 0.7)
        assert low > 90.0, "this width/stackup genuinely cannot reach 90 ohm"
        with pytest.raises(ImpedanceUnreachable) as excinfo:
            estimate_gap(90.0, 0.34, 0.7)
        assert "outside" in str(excinfo.value)

    def test_the_range_widens_as_the_trace_narrows(self) -> None:
        _, narrow_high = achievable_range_for(0.15, 0.2)
        _, wide_high = achievable_range_for(0.5, 0.2)
        assert narrow_high > wide_high


def achievable_range_for(width: float, height: float) -> tuple[float, float]:
    from aipcb.route.diffpair import achievable_range

    return achievable_range(width, height)


class TestCentreLineSplit:
    def test_offsets_run_the_same_way(self) -> None:
        """Shapely's offset_curve keeps direction; reversing it swaps the halves."""
        from aipcb.route.diffpair import split_centre_line

        centre = [(0.0, 0.0), (10.0, 0.0)]
        left, right = split_centre_line(centre, 0.5)
        assert left[0][0] < left[-1][0]
        assert right[0][0] < right[-1][0]

    def test_the_gap_is_the_pitch(self) -> None:
        from aipcb.route.diffpair import split_centre_line

        left, right = split_centre_line([(0.0, 0.0), (10.0, 0.0)], 0.6)
        assert abs(abs(left[0][1] - right[0][1]) - 0.6) < 1e-6

    def test_a_corner_is_mitred(self) -> None:
        """The inside of a bend cuts in and the outside swings wide."""
        from aipcb.route.diffpair import split_centre_line

        left, right = split_centre_line([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 0.6)
        inner = min(len_of(left), len_of(right))
        outer = max(len_of(left), len_of(right))
        assert inner < outer


def len_of(points: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(points))


@needs_kicad_libraries
class TestDifferentialPairs:
    def _route(self, name: str, tmp_path: Path):
        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / name / "design.yaml", out_dir=tmp_path, report=report
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        return route_board(board, result.netlist, report, topologies=topologies), report

    def test_a_clean_pair_is_routed_coupled(self, tmp_path: Path) -> None:
        routed, _ = self._route("diff-pair", tmp_path)
        assert len(routed.pairs) == 1
        pair = routed.pairs[0]
        assert {pair.positive, pair.negative} == {"DIFF_P", "DIFF_N"}

    def test_a_coupled_pair_has_matched_halves(self, tmp_path: Path) -> None:
        """The point of routing one centre-line: the halves come out the same length."""
        from aipcb.route.diffpair import skew_of

        routed, _ = self._route("diff-pair", tmp_path)
        pair = routed.pairs[0]
        halves = [
            result
            for result, _, _ in routed.with_endpoints()
            if result.net in (pair.positive, pair.negative)
        ]
        assert len(halves) == 2
        assert pair.max_skew is not None
        assert skew_of(halves[0], halves[1]) <= pair.max_skew

    def test_the_gap_comes_from_the_net_class(self, tmp_path: Path) -> None:
        routed, _ = self._route("diff-pair", tmp_path)
        assert routed.pairs[0].gap == 0.2

    def test_a_pair_it_cannot_couple_falls_back_and_says_why(self, tmp_path: Path) -> None:
        """Silently routing something that only looks like a pair would be worse."""
        routed, report = self._route("usb-port", tmp_path)
        excuses = [d for d in report if d.code == "diff-pair-not-coupled"]
        assert excuses, "usb-port's pairs cannot be coupled on that board"
        assert all(d.message and d.hint for d in excuses)
        assert not routed.pairs

    def test_an_ambiguous_pair_is_left_alone(self, tmp_path: Path) -> None:
        from aipcb.route.diffpair import find_pairs
        from aipcb.route.obstacles import extract_obstacles

        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / "diff-pair" / "design.yaml",
            out_dir=tmp_path, report=report,
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        environment = extract_obstacles(board)
        pairs = find_pairs(result.netlist, environment, report)
        assert [p.key() for p in pairs] == ["DIFF_N+DIFF_P"]

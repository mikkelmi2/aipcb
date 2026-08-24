# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
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
from typing import ClassVar

import pytest

from aipcb.cli_route import BETA_DOCS_URL
from aipcb.compile.build import build_design
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import dump, parse
from aipcb.route.emit import attach_copper, track_uuid
from aipcb.route.funnel import orient_portals, signed_area, tighten
from aipcb.route.model import Pass, RouteTopology, ViaHop, parse_node
from aipcb.route.obstacles import convex_hull, extract_obstacles, inflate
from aipcb.route.plan import route_board, spanning_routes
from aipcb.route.stretch import RouteRules, StretchError, prepare, side_point, stretch_route
from aipcb.route.triangulate import (
    Triangulation,
    _inside,
    _link,
    build_triangulation,
    reduce_crossings,
)

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

EXAMPLES = (
    "led-blinker",
    "ldo-supply",
    "usb-port",
    "routing-demo",
    "diff-pair",
    "mcu-4layer",
    "congestion",
)

#: Violations an example is allowed to keep, with the reason. Empty for all but one.
#: `diff-pair` ends with a ground track running between the two pads of its supply
#: header at minimum clearance, which leaves a channel of laminate thin enough that
#: KiCad calls it a copper sliver. It is a manufacturability advisory rather than a
#: rule violation -- the clearance itself is met -- and closing it needs a pad-entry
#: pattern the router does not build. Listed here rather than waved away, so that
#: any *other* violation on any example still fails the suite.
ALLOWED_VIOLATIONS: dict[str, set[str]] = {"diff-pair": {"copper_sliver"}}


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


class TestPointLocation:
    """M19a: the vectorised candidate test, and the tie-break it has to reproduce.

    `locate_many` used to batch the R-tree query and then run a scalar Python loop
    over the candidates, trying them in ascending triangle index so that a point on
    a shared edge landed in the lower-numbered one. The vectorised version computes
    the same three orientations over whole arrays, where "lowest index wins" does
    not fall out for free -- so it is asserted here rather than assumed.
    """

    @staticmethod
    def _two_triangles() -> Triangulation:
        """A unit square split into triangles 0 and 1 across the diagonal (0,0)-(1,1)."""
        return _link(
            [
                ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
                ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            ]
        )

    def test_a_point_on_a_shared_edge_lands_in_the_lower_triangle(self) -> None:
        mesh = self._two_triangles()
        on_the_diagonal = (0.5, 0.5)
        assert _inside(on_the_diagonal, mesh.triangles[0])
        assert _inside(on_the_diagonal, mesh.triangles[1]), (
            "the case is only a tie-break if both triangles contain the point"
        )
        assert mesh.locate_many([on_the_diagonal]) == [0]

    def test_the_tie_break_is_index_order_and_not_query_order(self) -> None:
        """The same two triangles with their indices swapped answer the other way."""
        swapped = _link(
            [
                ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
                ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
            ]
        )
        assert swapped.locate_many([(0.5, 0.5)]) == [0]

    def test_locate_and_locate_many_agree_including_on_edges(self) -> None:
        """The docstring promise, on the points most likely to break it."""
        mesh = self._two_triangles()
        probes = [
            (0.5, 0.5),            # the shared diagonal
            (0.0, 0.0),            # a shared vertex
            (1.0, 1.0),            # the other shared vertex
            (0.5, 0.5 + 1e-15),    # a hair off the diagonal, either side
            (0.5, 0.5 - 1e-15),
            (0.75, 0.25),          # squarely inside triangle 0
            (0.25, 0.75),          # squarely inside triangle 1
            (0.5, 0.0),            # on the outer boundary
            (5.0, 5.0),            # nowhere near
        ]
        assert mesh.locate_many(probes) == [mesh.locate(p) for p in probes]

    def test_a_point_outside_every_triangle_is_none(self) -> None:
        mesh = self._two_triangles()
        assert mesh.locate_many([(5.0, 5.0), (0.75, 0.25)]) == [None, 0]

    def test_no_points_is_no_answers(self) -> None:
        assert self._two_triangles().locate_many([]) == []

    def test_it_agrees_with_the_scalar_loop_on_a_real_board(self) -> None:
        """The corpus's own geometry, against the implementation M19a replaced."""
        environment = extract_obstacles(
            parse(
                (REPO_ROOT / "tests" / "golden" / "mcu-4layer" / "mcu-4layer.kicad_pcb")
                .read_text(encoding="utf-8")
            )
        )
        mesh = build_triangulation(
            environment, environment.blocking(frozenset(), "F.Cu", clearance=0.2)
        )
        probes = []
        for a, b, c in mesh.triangles:
            probes.append(((a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3))
            probes.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
            probes.append(a)
        assert probes, "the board has to triangulate for this to test anything"
        expected = [
            next(
                (i for i in sorted(mesh._candidates(p)) if _inside(p, mesh.triangles[i])),
                None,
            )
            for p in probes
        ]
        assert mesh.locate_many(probes) == expected


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

    def test_the_pads_a_route_lands_on_do_not_block_it(self, tmp_path: Path) -> None:
        """Its own two pads are open; its net's *other* pads are not.

        A track may legally overlap copper of its own net, but one that clips a pad
        it is merely passing leaves a crescent a few microns wide -- a copper sliver.
        So only the two pads the route actually lands on are open to it.
        """
        _, _, board = self._board("usb-port", tmp_path)
        environment = extract_obstacles(board)
        open_pads = frozenset({"J1.1"})
        blocking = {
            o.name for o in environment.blocking("VBUS", "F.Cu", open_pads=open_pads)
        }
        assert "J1.1" not in blocking
        assert any(name.startswith("C1.1") for name in blocking), (
            "another VBUS pad the route is not landing on should still block"
        )

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
        environment, triangulation = prepare(
            board, "USB_DP", "F.Cu", rules, frozenset({"J1.3", "R1.1"})
        )
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "R1.1"}
        )
        result = stretch_route(route, environment, triangulation, rules)
        direct = math.dist(
            environment.pad_centres[environment.resolve_pad("J1.3") or ""],
            environment.pad_centres[environment.resolve_pad("R1.1") or ""],
        )
        assert result.copper_length < direct * 1.25, (
            f"{result.copper_length:.1f} mm against a {direct:.1f} mm straight line"
        )

    def test_stretching_is_deterministic(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "R1.1"}
        )
        runs = []
        for _ in range(2):
            environment, triangulation = prepare(
                board, "USB_DP", "F.Cu", rules, frozenset({"J1.3", "R1.1"})
            )
            runs.append(stretch_route(route, environment, triangulation, rules).legs[0].points)
        assert runs[0] == runs[1]

    def test_an_unreachable_endpoint_is_reported(self, tmp_path: Path) -> None:
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        environment, triangulation = prepare(
            board, "USB_DP", "F.Cu", rules, frozenset({"J1.3"})
        )
        route = RouteTopology.model_validate(
            {"net": "USB_DP", "from": "J1.3", "to": "Q9.1"}
        )
        with pytest.raises(StretchError, match="not a pad"):
            stretch_route(route, environment, triangulation, rules)

    def test_a_via_hop_needs_geometry_for_the_layer_it_lands_on(
        self, tmp_path: Path
    ) -> None:
        """One triangulation is one layer; a route that leaves it needs the other."""
        _, _, board = self._board("usb-port", tmp_path)
        rules = RouteRules()
        environment, triangulation = prepare(
            board, "USB_DP", "F.Cu", rules, frozenset({"J1.3", "R1.1"})
        )
        route = RouteTopology.model_validate(
            {
                "net": "USB_DP", "from": "J1.3", "to": "R1.1",
                "passes": [{"kind": "via", "to_layer": "B.Cu"}],
            }
        )
        with pytest.raises(StretchError, match="no routable area"):
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
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        routed = route_board(board, result.netlist, report, topologies=topologies)
        attach_copper(board, routed.connections, sorted(result.netlist.nets))
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
        allowed = ALLOWED_VIOLATIONS.get(name, set())
        violations = [
            f"[{v['severity']}] {v['type']}: {v['description']}"
            for v in drc["violations"]
            if v["type"] not in allowed
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

    @pytest.mark.parametrize("name", EXAMPLES)
    def test_every_example_routes_completely(self, name: str, tmp_path: Path) -> None:
        """The end-to-end claim: source in, a finished board out.

        Every one of them, including the two that could not be routed at all before
        M8 -- `led-blinker`'s DIP escape and `usb-port`'s 0.65 mm-pitch receptacle
        both need a second layer, and now get one.
        """
        routed, drc = self._route(name, tmp_path)
        assert not routed.failed, [f"{f.key()}: {f.reason}" for f in routed.failed]
        assert not drc["unconnected_items"], "every connection should be copper"
        # M13a's invariant, asserted per example rather than only inside the router:
        # two nets' copper in one place is a short circuit, and no board here has one.
        assert not routed.crossings, [c.describe() for c in routed.crossings]

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
            attach_copper(board, routed.connections, sorted(result.netlist.nets))
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
@needs_kicad_cli
class TestBetaNotice:
    """The router is beta, and says so once -- to a human, never into a pipe.

    The label is information, not decoration, so it has two forms and they must not
    leak into each other: one line on stderr for a person, and a ``maturity`` field
    in the report for anything reading the JSON.
    """

    #: Every key ``route all --json`` emitted before the maturity field existed.
    #: ``stitching`` is conditional on the design declaring a stitching pattern, so
    #: it is excluded here rather than asserted absent.
    ROUTING_KEYS: ClassVar[set[str]] = {
        "routed", "failed", "length_mm", "vias", "layers", "nets", "iterations",
        "converged", "handed_over", "fanout", "pairs", "transitions", "crossings",
        "manual", "segments",
        # M16b. `crossings` is two nets in one place; this is one connection
        # meeting itself. Both are lists of sentences and both are empty on a
        # board worth having.
        "self_crossings",
        # M17a/M17b. What the via pass took back, and what it looked at and left
        # alone: a candidate that is rejected is a number this reports rather than
        # a decision it swallows.
        "reclaimed",
    }

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", *args],
            capture_output=True, text=True, check=False,
        )

    def test_the_notice_is_one_line_on_stderr_with_a_link(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        result = self._run("all", str(design), "--out", str(tmp_path))
        notices = [line for line in result.stderr.splitlines() if "beta" in line]
        assert len(notices) == 1, result.stderr
        assert BETA_DOCS_URL in notices[0]
        assert "beta" not in result.stdout

    def test_route_check_says_it_too(self) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        result = self._run("check", str(design))
        assert sum("beta" in line for line in result.stderr.splitlines()) == 1

    def test_json_carries_the_label_structurally_and_not_in_prose(
        self, tmp_path: Path
    ) -> None:
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        result = self._run("all", str(design), "--out", str(tmp_path), "--json")
        assert result.stderr == "", "a machine consumer gets no prose"
        payload = json.loads(result.stdout)
        assert payload["routing"]["maturity"] == "beta"

    def test_json_is_schema_stable_apart_from_the_new_field(
        self, tmp_path: Path
    ) -> None:
        """The payload's shape is part of its contract, so additions are deliberate.

        `maturity` arrived in M15.1 and `self_crossings` in M16b; both are listed in
        `ROUTING_KEYS` above rather than waved past, so that the next field to
        appear has to be added on purpose.
        """
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        payload = json.loads(
            self._run("all", str(design), "--out", str(tmp_path), "--json").stdout
        )
        assert set(payload) == {"ok", "counts", "diagnostics", "routing"}
        keys = set(payload["routing"]) - {"stitching"}
        assert keys - {"maturity"} == self.ROUTING_KEYS
        assert "maturity" in keys

    def test_route_check_json_carries_it_as_well(self) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        result = self._run("check", str(design), "--json")
        assert result.stderr == ""
        assert json.loads(result.stdout)["routes"]["maturity"] == "beta"


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

    def test_avoiding_narrow_gaps_costs_nothing_in_completeness(
        self, tmp_path: Path
    ) -> None:
        """Both settings finish the board; the knob is about *how*, not whether.

        In M7 this was the measurement that justified the default -- shortest-only
        stranded a connection. With a second layer to escape to, both settings route
        `led-blinker` completely, and what congestion buys is visible in the numbers
        below instead.
        """
        greedy = self._route("led-blinker", tmp_path, 0.0)
        careful = self._route("led-blinker", tmp_path, 1.0)
        assert not greedy.failed and not careful.failed

    def test_and_uses_no_more_copper_and_no_more_vias(self, tmp_path: Path) -> None:
        """Spending open space first leaves the tight gaps for what needs them."""
        greedy = self._route("led-blinker", tmp_path, 0.0)
        careful = self._route("led-blinker", tmp_path, 1.0)
        assert careful.total_length <= greedy.total_length
        assert len(careful.vias) <= len(greedy.vias)

    def test_congestion_is_deterministic(self, tmp_path: Path) -> None:
        first = self._route("usb-port", tmp_path / "a", 1.0)
        second = self._route("usb-port", tmp_path / "b", 1.0)
        assert [r.points for r in first.routed] == [r.points for r in second.routed]
        assert [v.point for v in first.vias] == [v.point for v in second.vias]

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
        routed, _ = self._route("diff-pair", tmp_path)
        pair = routed.pairs[0]
        halves = [
            connection
            for connection in routed.connections
            if connection.net in (pair.positive, pair.negative)
        ]
        assert len(halves) == 2
        assert pair.max_skew is not None
        assert abs(halves[0].length - halves[1].length) <= pair.max_skew

    def test_the_gap_comes_from_the_net_class(self, tmp_path: Path) -> None:
        routed, _ = self._route("diff-pair", tmp_path)
        assert routed.pairs[0].gap == 0.2

    def test_a_pair_it_cannot_couple_falls_back_and_says_why(self, tmp_path: Path) -> None:
        """Silently routing something that only looks like a pair would be worse.

        On `usb-port` the connector-side pair now couples; the device-side one does
        not, because the placer leaves its two series resistors 11 mm apart and a
        "pair" whose ends are 11 mm apart is two breakouts. What matters is that the
        refusal says so, with the measurement.
        """
        routed, report = self._route("usb-port", tmp_path)
        excuses = [d for d in report if d.code == "diff-pair-not-coupled"]
        assert excuses, "usb-port's device-side pair cannot be coupled on that board"
        assert all(d.message and d.hint for d in excuses)
        assert len(routed.pairs) == 1

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


# ---------------------------------------------------------------------------
# M13a: no route crosses another net
# ---------------------------------------------------------------------------


def _leg(net: str, layer: str, points, *, width: float = 0.2, name: str = "a>b"):
    from aipcb.route.stretch import StretchResult

    start, end = name.split(">")
    return StretchResult(
        net=net, layer=layer, points=list(points), width=width, start=start, end=end
    )


class TestCrossNetInvariant:
    """The check that would have caught M11's `_repair` defect the day it landed.

    Everything the router builds is tightened inside free space that already has
    the other nets' copper removed from it, so this should never find anything.
    That is the point: the construction is only as good as its inputs, and M13
    found an input that had been quietly losing polygons since M11c.
    """

    def test_two_nets_in_one_place_on_one_layer_is_a_crossing(self) -> None:
        from aipcb.route.invariant import crossing_nets
        from aipcb.route.stretch import RoutedConnection

        connections = [
            RoutedConnection(
                net="A", start="a", end="b",
                legs=[_leg("A", "F.Cu", [(0.0, 0.0), (10.0, 0.0)])],
            ),
            RoutedConnection(
                net="B", start="c", end="d",
                legs=[_leg("B", "F.Cu", [(5.0, -5.0), (5.0, 5.0)])],
            ),
        ]
        found = crossing_nets(connections)
        assert len(found) == 1
        assert {found[0].first, found[0].second} == {"A", "B"}
        assert found[0].layer == "F.Cu"
        assert found[0].area_mm2 == pytest.approx(0.04, rel=0.05)

    def test_the_same_crossing_on_two_layers_is_not_one(self) -> None:
        """Copper crosses in plan view all the time; only on one layer is it a short."""
        from aipcb.route.invariant import crossing_nets
        from aipcb.route.stretch import RoutedConnection

        connections = [
            RoutedConnection(
                net="A", start="a", end="b",
                legs=[_leg("A", "F.Cu", [(0.0, 0.0), (10.0, 0.0)])],
            ),
            RoutedConnection(
                net="B", start="c", end="d",
                legs=[_leg("B", "B.Cu", [(5.0, -5.0), (5.0, 5.0)])],
            ),
        ]
        assert crossing_nets(connections) == []

    def test_a_net_may_cross_itself(self) -> None:
        from aipcb.route.invariant import crossing_nets
        from aipcb.route.stretch import RoutedConnection

        connections = [
            RoutedConnection(
                net="A", start="a", end="b",
                legs=[_leg("A", "F.Cu", [(0.0, 0.0), (10.0, 0.0)])],
            ),
            RoutedConnection(
                net="A", start="c", end="d",
                legs=[_leg("A", "F.Cu", [(5.0, -5.0), (5.0, 5.0)])],
            ),
        ]
        assert crossing_nets(connections) == []

    def test_a_via_barrel_crosses_every_layer_it_passes(self) -> None:
        from aipcb.route.invariant import crossing_nets
        from aipcb.route.stretch import RoutedConnection, Via

        connections = [
            RoutedConnection(
                net="A", start="a", end="b",
                legs=[_leg("A", "In1.Cu", [(0.0, 0.0), (10.0, 0.0)])],
            ),
            RoutedConnection(
                net="B", start="c", end="d",
                vias=[
                    Via(
                        net="B", point=(5.0, 0.0), from_layer="F.Cu",
                        to_layer="B.Cu", diameter=0.6, drill=0.3, name="v",
                    )
                ],
            ),
        ]
        spans = {"F.Cu/B.Cu": ("F.Cu", "In1.Cu", "B.Cu")}
        assert crossing_nets(connections, barrel_layers=spans)
        # The same via declared as reaching only the front never meets In1.Cu.
        assert crossing_nets(connections, barrel_layers={"F.Cu/B.Cu": ("F.Cu",)}) == []

    def test_touching_at_a_boundary_is_not_a_crossing(self) -> None:
        """Two tracks exactly one clearance apart share a boundary, not an area."""
        from aipcb.route.invariant import crossing_nets
        from aipcb.route.stretch import RoutedConnection

        connections = [
            RoutedConnection(
                net="A", start="a", end="b",
                legs=[_leg("A", "F.Cu", [(0.0, 0.0), (10.0, 0.0)])],
            ),
            RoutedConnection(
                net="B", start="c", end="d",
                legs=[_leg("B", "F.Cu", [(0.0, 0.2), (10.0, 0.2)])],
            ),
        ]
        assert crossing_nets(connections) == []


class TestFinishedCopperIsNeverHidden:
    """The root cause of M11's `_repair` defect, at the level it actually lives.

    Finished copper is a *list* of obstacles and the free-space calculation wants a
    *dict* keyed by name. Two pieces that share a name therefore used to become
    one, and the loser disappeared from every triangulation built afterwards. A
    differential pair split across two layers by a via transition produces exactly
    that: one `RoutedConnection` per layer, both naming their coupled leg after the
    same two pair terminals.
    """

    def test_a_name_clash_does_not_lose_a_polygon(self) -> None:
        from aipcb.route.geometry import track_obstacles, with_copper
        from aipcb.route.obstacles import RoutingEnvironment

        base = RoutingEnvironment(outline=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)))
        placed = [
            *track_obstacles(
                _leg("N", "F.Cu", [(1.0, 1.0), (9.0, 1.0)]), "track:N/x>y", 0.1
            ),
            *track_obstacles(
                _leg("N", "B.Cu", [(1.0, 5.0), (9.0, 5.0)]), "track:N/x>y", 0.1
            ),
        ]
        assert len({o.name for o in placed}) == 1, "the fixture must actually clash"
        environment = with_copper(base, placed)
        kept = [o for o in environment.obstacles.values() if o.kind == "track"]
        assert len(kept) == len(placed)
        assert {next(iter(o.layers)) for o in kept} == {"F.Cu", "B.Cu"}

    def test_the_router_names_copper_by_layer_as_well(self, tmp_path: Path) -> None:
        """Belt and braces: the names the router hands out no longer clash at all."""
        import collections

        from aipcb.route.plan import _accept
        from aipcb.route.stack import stack_for
        from aipcb.route.stretch import RoutedConnection

        placed: list = []
        outcome = type("Outcome", (), {"connections": [], "total_length": 0.0})()
        stack = stack_for(None)
        for layer in ("F.Cu", "B.Cu"):
            _accept(
                outcome,
                placed,
                RoutedConnection(
                    net="N", start="x", end="y",
                    legs=[_leg("N", layer, [(1.0, 1.0), (9.0, 1.0)], name="x>y")],
                ),
                stack,
                trim=False,
            )
        counts = collections.Counter(o.name for o in placed)
        assert not [n for n, c in counts.items() if c > 1], counts


@needs_kicad_libraries
@needs_kicad_cli
class TestRepairDoesNotCrossAnotherNet:
    """M11's open defect, reproduced and closed.

    `examples/pcie-sata` deliberately leaves the A1-to-B17 presence-detect strap
    off the netlist, because routing it means crossing all three lane pairs. M11
    had it on the board briefly, and the second-pass repair in
    ``route/plan.py::_repair`` routed it straight through two already-placed
    `REFCLK` tracks -- two `tracks_crossing` errors from KiCad's own DRC.

    Putting the strap back is what reproduces it, so that is what this does. The
    board it produces is not a board anybody should ship; what matters is that the
    router either routes the strap legally or hands it over, and never lays copper
    on top of somebody else's.
    """

    def _with_strap(self, tmp_path: Path) -> Path:
        source = (REPO_ROOT / "examples" / "pcie-sata" / "design.yaml").read_text(
            encoding="utf-8"
        )
        library = (REPO_ROOT / "examples" / "library").as_posix()
        source = source.replace("../library/", f"{library}/")
        source = source.replace(
            "  P12V:\n    class: power",
            "  PRSNT:\n    class: signal\n"
            "    reason: the A1-B17 presence-detect strap, which has to cross the lane\n"
            "  P12V:\n    class: power",
        )
        source = source.replace(
            "      A13: REFCLKP", "      A1: PRSNT\n      B17: PRSNT\n      A13: REFCLKP"
        )
        assert "PRSNT" in source and "A1: PRSNT" in source
        design = tmp_path / "design.yaml"
        design.write_text(source, encoding="utf-8")
        return design

    def test_the_strap_never_crosses_the_lane(self, tmp_path: Path) -> None:
        from aipcb.kicad.cli import run_kicad

        report = Report()
        result = build_design(
            self._with_strap(tmp_path), out_dir=tmp_path / "out", report=report
        )
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        board = parse(board_path.read_text(encoding="utf-8"))
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        routed = route_board(board, result.netlist, report, topologies=topologies)

        assert any(d.code == "route-repaired" and d.context.get("net") == "PRSNT"
                   for d in report), "the strap should still take the repair path"
        assert not routed.crossings, [c.describe() for c in routed.crossings]

        attach_copper(board, routed.connections, sorted(result.netlist.nets))
        board_path.write_text(dump(board), encoding="utf-8")
        drc_path = tmp_path / "drc.json"
        run = run_kicad(
            "pcb", "drc", "--format", "json", "--severity-all",
            "-o", str(drc_path), str(board_path),
        )
        assert run.returncode == 0, f"{run.stdout}{run.stderr}"
        drc = json.loads(drc_path.read_text(encoding="utf-8"))
        crossing = [v for v in drc["violations"] if v["type"] == "tracks_crossing"]
        assert not crossing, [v["description"] for v in crossing]


# ---------------------------------------------------------------------------
# M16a: the cuts the triangulation never drew
# ---------------------------------------------------------------------------


class TestSpecialCuts:
    """The second diagonal of an adjacent triangle pair is a cut too.

    The toporouter postmortem's §A.6 is the find this milestone acts on: aipcb
    stated Maley's realizability criterion and then charged only the diagonals its
    triangulation happened to draw. A wire that enters a triangle through its two
    non-diagonal edges never crosses that diagonal at all -- it rounds the apex --
    and the room it has to do that is the *other* diagonal of the quadrilateral,
    which the CDT declined to draw precisely because it is the shorter one.
    """

    def test_a_convex_pair_yields_the_other_diagonal(self) -> None:
        from aipcb.route.triangulate import Diagonal, Triangulation, special_cuts

        # A unit square split by its own diagonal. Both apexes exist, the quad is
        # convex, so the flip diagonal is the square's other diagonal.
        square = Triangulation(
            triangles=[
                ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
                ((0.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (1.0, 1.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        cuts = special_cuts(square)
        assert len(cuts) == 1
        assert {cuts[0].a, cuts[0].b} == {(1.0, 0.0), (0.0, 1.0)}
        assert cuts[0].diagonal == 0

    def test_a_re_entrant_pair_yields_nothing(self) -> None:
        """A segment that leaves the free space is not a cut across it.

        The CDT is free to leave a re-entrant pair unflipped, and flipping it would
        put the new diagonal outside the shape. Charging wires to a segment that is
        not in the free area would invent congestion where there is none.
        """
        from aipcb.route.triangulate import Diagonal, Triangulation, special_cuts

        # (2, 0) is a reflex vertex of the quad (0,0) (4,1) (2,0) (1,-1), so the
        # apex-to-apex segment passes outside it entirely.
        dart = Triangulation(
            triangles=[
                ((0.0, 0.0), (2.0, 0.0), (4.0, 1.0)),
                ((0.0, 0.0), (1.0, -1.0), (2.0, 0.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (2.0, 0.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        assert special_cuts(dart) == []

    def test_apexes_on_one_side_yield_nothing(self) -> None:
        """Two triangles that overlap are not a quadrilateral at all.

        A triangulation never produces this, which is exactly why the guard is
        worth having: it means the derivation is correct on its own terms rather
        than only while its caller behaves.
        """
        from aipcb.route.triangulate import Diagonal, Triangulation, special_cuts

        overlapping = Triangulation(
            triangles=[
                ((0.0, 0.0), (2.0, 0.0), (1.0, 0.4)),
                ((0.0, 0.0), (1.0, 2.0), (2.0, 0.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (2.0, 0.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        assert special_cuts(overlapping) == []

    def test_the_apex_rounding_wire_is_charged(self) -> None:
        """The postmortem's exact scenario, end to end.

        A wire enters a triangle through one of its non-diagonal edges and leaves
        through the other, rounding the apex. It never touches the CDT diagonal --
        so the old accounting charged it nothing at all -- and it does cross the
        second diagonal of the pair, which is what M16a now charges.
        """
        from aipcb.route.field import LayerField
        from aipcb.route.triangulate import Diagonal, Triangulation

        # Two triangles sharing the long diagonal (0,0)-(4,0). The upper apex sits
        # just above it, so the flip diagonal (2, 0.3)-(2, -3) is much shorter than
        # the 4 mm diagonal it pairs with -- and it is the one that binds.
        pair = Triangulation(
            triangles=[
                ((0.0, 0.0), (4.0, 0.0), (2.0, 0.3)),
                ((0.0, 0.0), (2.0, -3.0), (4.0, 0.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (4.0, 0.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        cuts = pair.special_cuts()
        assert len(cuts) == 1
        assert cuts[0].length() == pytest.approx(3.3)
        assert cuts[0].length() < pair.gate_width(0)

        field_ = LayerField(
            layer="F.Cu",
            free=None,
            triangulation=pair,
            capacity=[pair.gate_width(0) + 0.2],
            used=[0.0],
            history=[0.0],
            special=cuts,
            special_capacity=[cuts[0].length() + 0.2],
            special_used=[0.0],
        )
        # In through the left edge, round the apex, out through the right edge.
        rounding = [(1.0, 0.1), (2.0, 0.2), (3.0, 0.1)]
        assert field_.cuts_crossed(rounding) == [], (
            "the wire never meets the CDT diagonal, which is the whole problem"
        )
        assert field_.special_cuts_crossed(rounding) == [0]

    def test_a_wire_that_does_cross_the_diagonal_still_is(self) -> None:
        """The new cut set adds to the old one rather than replacing it."""
        from aipcb.route.field import LayerField
        from aipcb.route.triangulate import Diagonal, Triangulation

        pair = Triangulation(
            triangles=[
                ((0.0, 0.0), (4.0, 0.0), (2.0, 0.3)),
                ((0.0, 0.0), (2.0, -3.0), (4.0, 0.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (4.0, 0.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        cuts = pair.special_cuts()
        field_ = LayerField(
            layer="F.Cu",
            free=None,
            triangulation=pair,
            capacity=[pair.gate_width(0) + 0.2],
            used=[0.0],
            history=[0.0],
            special=cuts,
            special_capacity=[cuts[0].length() + 0.2],
            special_used=[0.0],
        )
        assert field_.cuts_crossed([(2.0, 0.2), (2.0, -1.0)]) == [0]

    def test_over_subscription_is_reported_separately(self) -> None:
        from aipcb.route.field import LayerField
        from aipcb.route.triangulate import Diagonal, Triangulation

        pair = Triangulation(
            triangles=[
                ((0.0, 0.0), (4.0, 0.0), (2.0, 0.3)),
                ((0.0, 0.0), (2.0, -3.0), (4.0, 0.0)),
            ],
            diagonals=[Diagonal(0, (0.0, 0.0), (4.0, 0.0), (0, 1))],
            by_triangle={0: [0], 1: [0]},
        )
        cuts = pair.special_cuts()
        field_ = LayerField(
            layer="F.Cu",
            free=None,
            triangulation=pair,
            capacity=[10.0],
            used=[0.0],
            history=[0.0],
            special=cuts,
            special_capacity=[0.5],
            special_used=[0.9],
        )
        assert field_.over_subscribed() == []
        assert field_.over_subscribed_special() == [0]

    @needs_kicad_libraries
    def test_the_corpus_has_plenty_of_them(self, tmp_path: Path) -> None:
        """The measurement C2 asked for before anything was built.

        The candidate said to count how often a flip diagonal is *shorter* than the
        diagonal it pairs with, and to scope the work down to a tidy-up if the
        answer was near zero. It is not near zero -- 11% to 29% of diagonals per
        layer on the bundled corpus at M16 -- which is why the cuts are charged
        rather than merely documented. The bar here is deliberately loose: the
        claim under test is "this geometry is common", not a golden number.
        """
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.triangulate import free_space, triangulate_free

        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / "led-blinker" / "design.yaml",
            out_dir=tmp_path,
            report=report,
        )
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        environment = extract_obstacles(board)
        blocking = environment.blocking(frozenset(), "F.Cu", clearance=0.2)
        triangulation = triangulate_free(free_space(environment, blocking))
        cuts = triangulation.special_cuts()
        shorter = [
            cut
            for cut in cuts
            if cut.length() < triangulation.gate_width(cut.diagonal)
        ]
        assert len(shorter) / len(triangulation.diagonals) > 0.05, (
            f"{len(shorter)} shorter of {len(triangulation.diagonals)} diagonals"
        )


# ---------------------------------------------------------------------------
# M16b: exposure E2, a route against itself
# ---------------------------------------------------------------------------


class TestSelfCrossingInvariant:
    """Nothing anywhere asked whether one connection crosses itself.

    `crossing_nets` skips same-net pairs and must -- two connections of one net are
    supposed to meet. A single funnel output is simple by construction; a route that
    changes layer is a concatenation of several, and that argument stops at the
    join. The toporouter's tightener produced exactly this class of geometry, and
    its arc-loop checks existed for no other reason.
    """

    def test_a_leg_that_crosses_itself_is_found(self) -> None:
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        # A bowtie: out, across, back, across again. The two segments cross in
        # their interiors, at a point that is in none of the polyline's own
        # coordinates -- which is exactly the case the diagnostic has to find, or
        # it points a reader at the wrong end of the wire.
        loop = _leg(
            "A", "F.Cu",
            [(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)],
        )
        found = self_crossings([RoutedConnection(net="A", start="a", end="b", legs=[loop])])
        assert [c.kind for c in found] == ["leg-not-simple"]
        assert "crosses itself" in found[0].describe()
        assert found[0].at == (1.0, 1.0)

    def test_a_leg_that_returns_to_a_vertex_names_that_vertex(self) -> None:
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        # Round a square and back out through the corner it came in by.
        loop = _leg(
            "A", "F.Cu",
            [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0), (2.0, 0.0)],
        )
        found = self_crossings([RoutedConnection(net="A", start="a", end="b", legs=[loop])])
        assert [c.kind for c in found] == ["leg-not-simple"]
        assert found[0].at == (1.0, 0.0)

    def test_a_simple_leg_is_not(self) -> None:
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        straight = _leg("A", "F.Cu", [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)])
        assert self_crossings(
            [RoutedConnection(net="A", start="a", end="b", legs=[straight])]
        ) == []

    def test_two_legs_meeting_on_one_layer_is_found(self) -> None:
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        connection = RoutedConnection(
            net="A", start="a", end="b",
            legs=[
                _leg("A", "F.Cu", [(0.0, 0.0), (4.0, 0.0)], name="a>via"),
                _leg("A", "F.Cu", [(2.0, -1.0), (2.0, 1.0)], name="via>b"),
            ],
        )
        found = self_crossings([connection])
        assert [c.kind for c in found] == ["legs-meet"]
        assert found[0].at == (2.0, 0.0)

    def test_legs_that_only_share_the_via_they_join_at_are_not(self) -> None:
        """The normal case: a route hops layer and comes back, meeting at the vias."""
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        connection = RoutedConnection(
            net="A", start="a", end="b",
            legs=[
                _leg("A", "F.Cu", [(0.0, 0.0), (2.0, 0.0)], name="a>v1"),
                _leg("A", "B.Cu", [(2.0, 0.0), (2.0, 4.0)], name="v1>v2"),
                _leg("A", "F.Cu", [(2.0, 4.0), (5.0, 4.0)], name="v2>b"),
            ],
        )
        assert self_crossings([connection]) == []

    def test_legs_on_different_layers_may_cross_freely(self) -> None:
        from aipcb.route.invariant import self_crossings
        from aipcb.route.stretch import RoutedConnection

        connection = RoutedConnection(
            net="A", start="a", end="b",
            legs=[
                _leg("A", "F.Cu", [(0.0, 0.0), (4.0, 0.0)], name="a>v1"),
                _leg("A", "B.Cu", [(2.0, -1.0), (2.0, 1.0)], name="v1>b"),
            ],
        )
        assert self_crossings([connection]) == []

    def test_the_two_findings_carry_different_severities(self) -> None:
        """Copper twice over is waste; a leg crossing itself is a defect."""
        from aipcb.route.invariant import check_no_self_crossings
        from aipcb.route.stretch import RoutedConnection

        report = Report()
        check_no_self_crossings(
            [
                RoutedConnection(
                    net="A", start="a", end="b",
                    legs=[
                        _leg("A", "F.Cu", [(0.0, 0.0), (4.0, 0.0)], name="a>v"),
                        _leg("A", "F.Cu", [(2.0, -1.0), (2.0, 1.0)], name="v>b"),
                    ],
                ),
                RoutedConnection(
                    net="B", start="c", end="d",
                    legs=[
                        _leg(
                            "B", "F.Cu",
                            [(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)],
                            name="c>d",
                        )
                    ],
                ),
            ],
            report,
        )
        codes = {d.code: d.severity.name for d in report}
        assert codes["route-doubles-back"] == "WARNING"
        assert codes["route-crosses-itself"] == "ERROR"

    @needs_kicad_libraries
    def test_the_corpus_carries_no_doubling(self, tmp_path: Path) -> None:
        """The guard that found it is the guard that keeps it fixed.

        From M16b to M17b this asserted the count was exactly *one*:
        `examples/pcie-sata`'s GND connection `U1.17>U1.49` travelled four
        millimetres east, hopped to B.Cu for half a millimetre, hopped straight
        back, and retraced its own path home -- about eight millimetres of copper
        and two vias that bought nothing.

        M17's via pass asked whether that span fitted on F.Cu in one leg. It did,
        and the connection gave back two vias and 17.116 mm of copper. The
        assertion is inverted rather than deleted, because a fix with no guard on
        it is a fix until somebody changes the cost model.
        """
        from aipcb.route.pipeline import route_design

        report = Report()
        done = route_design(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", tmp_path, report
        )
        assert done.routed.self_crossings == [], [
            c.describe() for c in done.routed.self_crossings
        ]
        assert not [d for d in report if d.code == "route-doubles-back"]


# ---------------------------------------------------------------------------
# M17a and M17b: the vias the finished board did not need
# ---------------------------------------------------------------------------


class TestViaReclaim:
    """`aipcb.route.tidy`, whose whole job is to be unable to make a board worse.

    A span is collapsed only when it comes out with fewer vias and no more copper,
    and only when the M16a cut model -- both families -- says the corridors it now
    crosses still carry what they carry. Everything below asks one of those
    guarantees a question with a known answer.
    """

    def _field(self, capacity: float = 10.0):
        """A one-layer field whose single cut is a 4 mm gate across the middle."""
        from shapely.geometry import Polygon as ShapelyPolygon

        from aipcb.route.field import LayeredField, LayerField
        from aipcb.route.stack import stack_for
        from aipcb.route.triangulate import triangulate_free

        free = ShapelyPolygon([(0.0, 0.0), (8.0, 0.0), (8.0, 4.0), (0.0, 4.0)])
        triangulation = triangulate_free(free)
        layer = LayerField(
            layer="F.Cu",
            free=free,
            triangulation=triangulation,
            capacity=[capacity] * len(triangulation.diagonals),
            used=[0.0] * len(triangulation.diagonals),
            history=[0.0] * len(triangulation.diagonals),
        )
        from aipcb.route.costs import DEFAULT_COSTS

        return LayeredField(
            stack=stack_for(None, DEFAULT_COSTS),
            reference_clearance=0.2,
            layers={"F.Cu": layer},
        )

    def test_a_swap_that_would_over_subscribe_a_cut_is_refused(self) -> None:
        """The arbiter M16a's soundness work exists for, asked directly.

        The board's cuts are given a capacity of a third of a millimetre -- less
        than one track plus its clearance -- so any leg crossing one puts it over.
        The legs being removed cross nothing, so the swap has nowhere to give the
        demand back from, and it must be refused.
        """
        from aipcb.route.tidy import Occupancy

        field_ = self._field(capacity=0.3)
        occupancy = Occupancy(field_=field_)
        occupancy.used["F.Cu"] = [0.0] * len(field_.layers["F.Cu"].capacity)
        occupancy.special_used["F.Cu"] = []
        across = _leg("A", "F.Cu", [(0.5, 0.5), (7.5, 3.5)], name="a>b")
        aside = _leg("A", "B.Cu", [(0.5, 0.5), (0.6, 0.6)], name="a>v")
        assert occupancy.admits([aside], across, 0.45) is False

    def test_a_swap_a_wide_gate_can_carry_is_allowed(self) -> None:
        from aipcb.route.tidy import Occupancy

        field_ = self._field(capacity=10.0)
        occupancy = Occupancy(field_=field_)
        occupancy.used["F.Cu"] = [0.0] * len(field_.layers["F.Cu"].capacity)
        occupancy.special_used["F.Cu"] = []
        across = _leg("A", "F.Cu", [(0.5, 0.5), (7.5, 3.5)], name="a>b")
        aside = _leg("A", "B.Cu", [(0.5, 0.5), (0.6, 0.6)], name="a>v")
        assert occupancy.admits([aside], across, 0.45) is True

    @needs_kicad_libraries
    def test_the_pcie_board_gives_back_the_vias_it_was_measured_to_waste(
        self, tmp_path: Path
    ) -> None:
        """The M17a number, pinned to the board it was measured on.

        Two spans on `examples/pcie-sata` collapse: GND `U1.17>U1.49`, which is the
        M16b E2 finding, and GND `U1.49>U1.42`. Four vias and 30.285 mm of copper.
        The rest of that board's layer changes were tested and kept, which is the
        finding rather than a shortfall -- see the M17 report.
        """
        from aipcb.route.pipeline import route_design

        done = route_design(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", tmp_path, Report()
        )
        reclaimed = done.routed.reclaimed
        assert reclaimed is not None
        assert reclaimed.vias_removed == 4
        assert reclaimed.retraces_removed == 1
        assert round(reclaimed.copper_saved, 3) == 30.285
        assert [c.connection for c in reclaimed.collapses] == [
            "U1.17>U1.49", "U1.49>U1.42"
        ]
        assert len(done.routed.vias) == 38

    @needs_kicad_libraries
    def test_the_via_pass_is_idempotent(self, tmp_path: Path) -> None:
        """Run it twice, and the second run has nothing to say.

        Asserted rather than argued. The pass mutates the connections it is given,
        so "it converged" is a property of the code and not of the shape of the
        problem, and the cheapest way to know is to ask it again.
        """
        from aipcb.route.costs import DEFAULT_COSTS
        from aipcb.route.field import build_field
        from aipcb.route.geometry import connection_obstacles, edge_clearance_for
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.pipeline import route_design
        from aipcb.route.stack import stack_for
        from aipcb.route.tidy import reclaim_vias

        done = route_design(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", tmp_path, Report()
        )
        netlist = done.build.netlist
        stack = stack_for(netlist.layout, DEFAULT_COSTS)
        base = extract_obstacles(
            done.board, edge_clearance=edge_clearance_for(netlist)
        )
        field_ = build_field(
            base,
            stack,
            layout=netlist.layout,
            origin=netlist.layout.origin_mm if netlist.layout else (0.0, 0.0),
        )
        placed = [
            obstacle
            for connection in done.routed.connections
            for obstacle in connection_obstacles(connection, stack)
        ]
        again = reclaim_vias(
            done.routed.connections, base, placed, netlist, stack, field_, 1.0,
            Report(), allowed=tuple(stack.copper),
        )
        assert again.collapses == []
        assert again.vias_removed == 0

    @needs_kicad_libraries
    def test_a_controlled_impedance_net_is_left_alone(self, tmp_path: Path) -> None:
        """Verified, not assumed -- the milestone asked for that in those words.

        Removing a via changes the geometry M11d's standoff rule and M11e's audit
        measured, so a controlled-impedance class is skipped by class. Two halves,
        because on this corpus the first one passes for the wrong reason: every
        controlled-impedance connection on `pcie-sata` carries either three coupled
        legs and no via of its own, or one via and no legs at all -- the holes belong
        to the declared pair transition, which this pass never looks at. So the board
        is checked *and* the guard is handed a candidate it has to refuse.
        """
        from aipcb.route.costs import DEFAULT_COSTS
        from aipcb.route.field import build_field
        from aipcb.route.geometry import (
            class_for,
            connection_obstacles,
            edge_clearance_for,
        )
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.pipeline import route_design
        from aipcb.route.stack import stack_for
        from aipcb.route.stretch import RoutedConnection, Via
        from aipcb.route.tidy import reclaim_vias

        done = route_design(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", tmp_path, Report()
        )
        netlist = done.build.netlist
        controlled = sorted(
            {
                connection.net
                for connection in done.routed.connections
                if class_for(netlist, connection.net).controlled_impedance
            }
        )
        assert controlled, "the board must carry a controlled-impedance class"
        reclaimed = done.routed.reclaimed
        touched = {c.net for c in (reclaimed.collapses if reclaimed else [])}
        assert not (touched & set(controlled))

        # And now the guard itself, with a candidate built to be irresistible: a
        # 0.02 mm excursion to B.Cu and straight back, on open board, which any
        # ordinary net would give up in an instant.
        net = controlled[0]
        stack = stack_for(netlist.layout, DEFAULT_COSTS)
        base = extract_obstacles(
            done.board, edge_clearance=edge_clearance_for(netlist)
        )
        field_ = build_field(
            base,
            stack,
            layout=netlist.layout,
            origin=netlist.layout.origin_mm if netlist.layout else (0.0, 0.0),
        )
        anchor = next(iter(sorted(base.pad_centres)))
        here = base.pad_centres[anchor]
        there = (here[0] + 0.02, here[1])
        bait = RoutedConnection(
            net=net,
            start=anchor,
            end=anchor,
            legs=[
                _leg(net, "F.Cu", [here, there], name=f"{anchor}>via:x"),
                _leg(net, "B.Cu", [there, there], name="via:x>via:y"),
                _leg(net, "F.Cu", [there, here], name=f"via:y>{anchor}"),
            ],
            vias=[
                Via(net=net, point=there, from_layer="F.Cu", to_layer="B.Cu", name="x"),
                Via(net=net, point=there, from_layer="B.Cu", to_layer="F.Cu", name="y"),
            ],
        )
        result = reclaim_vias(
            [bait],
            base,
            list(connection_obstacles(bait, stack)),
            netlist,
            stack,
            field_,
            1.0,
            Report(),
            allowed=tuple(stack.copper),
        )
        assert result.skipped_controlled == 1
        assert result.collapses == []
        assert len(bait.vias) == 2


# ---------------------------------------------------------------------------
# M16b: exposure E3, the premise about coordinate range
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestScaleRobustness:
    """What killed the toporouter, asked of this router and dated.

    gEDA PCB converted its base units to nanometres, its triangulation library's
    in-circle predicate stopped being reliable in the new coordinate range, and
    edge-flipping recursed ten thousand deep on a board with two resistors. ADR
    0006 refused a hand-rolled CDT for exactly this reason and takes GEOS's through
    Shapely, and coordinates snap to 1e-6 mm -- which is almost certainly fine at
    board scale. "Almost certainly" is the state `CLAUDE.md`'s premise rule exists
    to end.

    Measured at M16 on Shapely 2.1.2 / GEOS 3.13.1: identical copper out to an origin
    of 50 000 mm, and the topology moves at 100 000 mm.

    2 147 mm appears below because it is KiCad's own limit, which was measured too
    rather than assumed. Board coordinates are 32-bit nanometres, and KiCad 9.0.8
    does not refuse one that overflows -- it clamps it. Given the boards this test
    writes at 2 147 mm, 5 000 mm and 100 000 mm, `kicad-cli pcb drc` reports the
    same position back for all three, `x: 2147.483637`, which is INT32_MAX
    nanometres. So the router is exact across twenty-three times everything a KiCad
    file can hold, and diverges only well outside the range the question can be
    asked in.
    """

    #: Where the board's frame is put, in millimetres from the KiCad page origin.
    #: 2 147 mm is the largest coordinate a KiCad file can hold at all.
    ORIGINS: ClassVar[tuple[float, ...]] = (100.0, 2147.0, 50000.0)

    def _design(self, tmp_path: Path, origin: float) -> Path:
        import yaml

        source = REPO_ROOT / "examples" / "led-blinker" / "design.yaml"
        design = yaml.safe_load(source.read_text(encoding="utf-8"))
        design["libraries"] = [
            str((source.parent / name).resolve()) for name in design["libraries"]
        ]
        design["layout"]["origin_mm"] = [origin, origin]
        out = tmp_path / f"at-{origin:.0f}"
        out.mkdir()
        path = out / "design.yaml"
        path.write_text(yaml.safe_dump(design, sort_keys=False), encoding="utf-8")
        return path

    def test_the_same_board_comes_out_wherever_it_sits(self, tmp_path: Path) -> None:
        from aipcb.route.pipeline import route_design

        shapes: list[tuple[float, tuple[object, ...]]] = []
        for origin in self.ORIGINS:
            design = self._design(tmp_path, origin)
            report = Report()
            done = route_design(design, design.parent / "out", report)
            assert not done.routed.failed, [f.key() for f in done.routed.failed]
            # The topology, expressed so it survives translation: each connection's
            # layers, its via count, and the length of its copper.
            shape = tuple(
                (
                    connection.net,
                    connection.start,
                    connection.end,
                    connection.layers,
                    len(connection.vias),
                    round(connection.copper_length, 6),
                )
                for connection in sorted(
                    done.routed.connections, key=lambda c: (c.net, c.start, c.end)
                )
            )
            shapes.append((origin, shape))

        first_origin, first = shapes[0]
        for origin, shape in shapes[1:]:
            assert shape == first, (
                f"the board routed differently at an origin of {origin:.0f} mm "
                f"than at {first_origin:.0f} mm"
            )

    def test_the_measurement_that_says_where_it_stops(self, tmp_path: Path) -> None:
        """The other half of a dated premise: the point where it *does* move.

        A premise that says "it works" and never says where it stops is not
        measured, it is asserted. At an origin of 100 000 mm this board routes 53
        segments and 271.8 mm instead of 46 and 262.7 -- still legal copper, no
        longer the same copper. That is 47 times outside anything KiCad can store,
        so it is a boundary rather than a bug; recorded so the next person can tell
        how much headroom the premise actually has.
        """
        from aipcb.route.pipeline import route_design

        design = self._design(tmp_path, 100000.0)
        report = Report()
        done = route_design(design, design.parent / "out", report)
        near = self._design(tmp_path, 100.0)
        baseline = route_design(near, near.parent / "out", Report())
        assert round(done.routed.total_length, 3) != round(
            baseline.routed.total_length, 3
        ), (
            "if this starts passing, GEOS got more robust and the note in this "
            "class needs re-measuring rather than deleting"
        )

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Multilayer routing: layers, via columns, cut capacity and negotiated congestion.

These are M8's own tests. The acceptance bar is the same as every milestone's --
DRC-clean copper from `kicad-cli`, byte-stable output -- but most of what M8 changed
is invisible at that level. A board can be perfectly legal and still have been
routed by a negotiation that never converged, or with a via placed by luck, or with
a plane quietly used as a signal layer. Each of those has a test here.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from aipcb.compile.build import build_design
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import dump, parse
from aipcb.model.layout import NetClass, Stackup
from aipcb.route.costs import DEFAULT_COSTS, CostModel
from aipcb.route.emit import attach_copper, drop_generated, generated_uuids, via_uuid
from aipcb.route.negotiate import (
    BASE_PRIORITY,
    PAIR_PRIORITY,
    default_priority,
    rip_up_weight,
)
from aipcb.route.plan import route_board
from aipcb.route.stack import stack_for

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries


def design(name: str) -> Path:
    return REPO_ROOT / "examples" / name / "design.yaml"


def route(name: str, out: Path, **kwargs: object):
    """Build and route one example, returning (routed board, netlist, board tree)."""
    report = Report()
    result = build_design(design(name), out_dir=out, report=report)
    board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    board = parse(board_path.read_text(encoding="utf-8"))
    topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
    routed = route_board(
        board, result.netlist, report, topologies=topologies, **kwargs
    )
    return routed, result.netlist, board, report


# ---------------------------------------------------------------------------
# the stackup as routing law
# ---------------------------------------------------------------------------


class TestStackup:
    def test_a_plane_is_not_a_signal_layer(self) -> None:
        stack = stack_for(None)
        assert stack.signal == ("F.Cu", "B.Cu")

        four = Stackup.model_validate(
            {
                "copper_layers": 4,
                "planes": [
                    {"layer": "In1.Cu", "net": "GND"},
                    {"layer": "In2.Cu", "net": "VCC"},
                ],
            }
        )
        assert four.signal_layers == ("F.Cu", "B.Cu")
        assert four.plane_layers == {"In1.Cu": "GND", "In2.Cu": "VCC"}

    def test_a_plane_on_a_layer_the_board_lacks_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not one of this 2-layer board"):
            Stackup.model_validate(
                {"copper_layers": 2, "planes": [{"layer": "In1.Cu", "net": "GND"}]}
            )

    def test_a_board_that_is_all_plane_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nowhere left to route"):
            Stackup.model_validate(
                {
                    "copper_layers": 2,
                    "planes": [
                        {"layer": "F.Cu", "net": "GND"},
                        {"layer": "B.Cu", "net": "VCC"},
                    ],
                }
            )

    def test_a_plane_costs_infinity_unless_a_class_opts_in(self) -> None:
        layout = _four_layer_layout()
        stack = stack_for(layout)
        ordinary = NetClass()
        assert stack.layer_penalty("In1.Cu", ordinary) == math.inf
        assert "In1.Cu" not in stack.layers_for(ordinary)

        opted_in = NetClass(prefer_layers=("In1.Cu",))
        assert stack.layer_penalty("In1.Cu", opted_in) == 0.0
        assert "In1.Cu" in stack.layers_for(opted_in)

    def test_layer_forbid_outranks_everything(self) -> None:
        stack = stack_for(_four_layer_layout())
        forbidden = NetClass(layer_forbid=("B.Cu",))
        assert "B.Cu" not in stack.layers_for(forbidden)
        assert stack.layer_penalty("B.Cu", forbidden) == math.inf

    def test_a_class_cannot_prefer_and_forbid_the_same_layer(self) -> None:
        with pytest.raises(ValueError, match="both preferred and forbidden"):
            NetClass.model_validate(
                {"prefer_layers": ["B.Cu"], "layer_forbid": ["B.Cu"]}
            )

    def test_a_via_blocks_every_layer_its_barrel_passes(self) -> None:
        """A through via on a four-layer board is a hole through all four."""
        stack = stack_for(_four_layer_layout())
        assert stack.barrel_span("F.Cu", "B.Cu") == (
            "F.Cu",
            "In1.Cu",
            "In2.Cu",
            "B.Cu",
        )
        assert stack.via_type("F.Cu", "B.Cu") == "through"

    def test_blind_vias_are_used_only_when_the_stackup_offers_them(self) -> None:
        through_only = stack_for(_four_layer_layout())
        assert through_only.via_type("F.Cu", "In1.Cu") == "through"
        assert through_only.barrel_span("F.Cu", "In1.Cu") == through_only.copper

        with_blind = stack_for(_four_layer_layout(via_types=("through", "blind")))
        assert with_blind.via_type("F.Cu", "In1.Cu") == "blind"
        assert with_blind.barrel_span("F.Cu", "In1.Cu") == ("F.Cu", "In1.Cu")

    def test_a_barrel_has_a_length_and_it_comes_from_the_stackup(self) -> None:
        """Length matching that ignores the barrel is matching the wrong thing."""
        stackup = Stackup.model_validate({"copper_layers": 2, "thickness_mm": 1.6})
        through = stackup.barrel_length_mm("F.Cu", "B.Cu")
        assert 1.4 < through < 1.6
        assert stackup.barrel_length_mm("F.Cu", "F.Cu") == 0.0

        thinner = Stackup.model_validate({"copper_layers": 2, "thickness_mm": 0.8})
        assert thinner.barrel_length_mm("F.Cu", "B.Cu") < through

    def test_a_direction_hint_costs_something_and_not_too_much(self) -> None:
        stack = stack_for(_four_layer_layout(direction={"F.Cu": "horizontal"}))
        assert stack.direction_penalty("F.Cu", 10.0, 0.0) == 0.0
        assert stack.direction_penalty("F.Cu", 0.0, 10.0) == pytest.approx(2.5)
        assert stack.direction_penalty("B.Cu", 0.0, 10.0) == 0.0


class _Probe(NamedTuple):
    board: object
    netlist: object


#: A board built to make one question visible: two nets that both want to cross the
#: same channel, and a `channel` millimetres wide gap for them to do it in.
_PRIORITY_PROBE = """
name: priority-probe
revision: A
libraries:
  - {library}/connectors.yaml
net_classes:
  loud:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    priority: 90
    rip_up: never
  quiet:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    priority: 10
    rip_up: normal
nets:
  X1: {{ class: {first} }}
  X2: {{ class: {second} }}
components:
  J1:
    part: CONN_BRK_1X04
    role: connector
    pins: {{ P1: X1, P2: X2, P3: X1, P4: X2 }}
  J2:
    part: CONN_BRK_1X04
    role: connector
    pins: {{ P1: X2, P2: X1, P3: X2, P4: X1 }}
layout:
  outline: {{ shape: rect, width_mm: 30.0, height_mm: 20.0 }}
  stackup: {{ copper_layers: 2, thickness_mm: 1.6 }}
  placement:
    margin_mm: 2.0
    keepouts:
      - region_mm: [10, 0, 20, {top}]
      - region_mm: [10, {bottom}, 20, 20]
    rules:
      - members: [J1]
        region_mm: [2, 3, 8.2, 17]
      - members: [J2]
        region_mm: [21, 3, 27.2, 17]
"""


def _priority_probe(tmp_path: Path, *, loud: str, channel: float) -> _Probe:
    library = (REPO_ROOT / "examples" / "library").resolve()
    source = _PRIORITY_PROBE.format(
        library=library,
        first="loud" if loud == "X1" else "quiet",
        second="loud" if loud == "X2" else "quiet",
        top=10 - channel / 2,
        bottom=10 + channel / 2,
    )
    path = tmp_path / f"probe-{loud}-{channel}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "design.yaml").write_text(source, encoding="utf-8")
    result = build_design(path / "design.yaml", out_dir=path, report=Report())
    board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    return _Probe(parse(board_path.read_text(encoding="utf-8")), result.netlist)


def _four_layer_layout(
    via_types: tuple[str, ...] = ("through",),
    direction: dict[str, str] | None = None,
):
    from aipcb.model.layout import Layout

    return Layout.model_validate(
        {
            "stackup": {
                "copper_layers": 4,
                "planes": [
                    {"layer": "In1.Cu", "net": "GND"},
                    {"layer": "In2.Cu", "net": "VCC"},
                ],
                "via_types": list(via_types),
                "preferred_direction": direction or {},
            }
        }
    )


# ---------------------------------------------------------------------------
# priority and rip-up
# ---------------------------------------------------------------------------


class TestPriority:
    def test_an_unset_priority_comes_from_the_class_name(self) -> None:
        """M7's ordering heuristic, expressed as the default of a source field."""
        assert default_priority("usb", NetClass(), pair=False) == 80
        assert default_priority("power", NetClass(), pair=False) == 60
        assert default_priority("signal", NetClass(), pair=False) == BASE_PRIORITY
        assert default_priority("whatever", NetClass(), pair=False) == BASE_PRIORITY

    def test_a_pair_outranks_its_class(self) -> None:
        assert default_priority("signal", NetClass(), pair=True) == PAIR_PRIORITY

    def test_a_stated_priority_wins(self) -> None:
        stated = NetClass(priority=17)
        assert default_priority("usb", stated, pair=True) == 17

    def test_rip_up_policy_orders_who_keeps_a_corridor(self) -> None:
        costs = DEFAULT_COSTS
        ordinary = rip_up_weight("normal", 50, costs)
        protected = rip_up_weight("protected", 50, costs)
        never = rip_up_weight("never", 50, costs)
        assert ordinary < protected < never
        # A protected net outranks an ordinary one of *any* priority, which is what
        # "low-priority traffic detours around it" means.
        assert protected > rip_up_weight("normal", 100, costs)


# ---------------------------------------------------------------------------
# the layered field
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestField:
    def _field(self, name: str, out: Path):
        from aipcb.route.field import build_field
        from aipcb.route.obstacles import extract_obstacles

        report = Report()
        result = build_design(design(name), out_dir=out, report=report)
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        environment = extract_obstacles(board)
        stack = stack_for(result.netlist.layout)
        field_ = build_field(
            environment,
            stack,
            reference_clearance=0.25,
            reference_width=0.3,
            via_radius=0.3,
            layout=result.netlist.layout,
            origin=result.netlist.layout.origin_mm if result.netlist.layout else (0, 0),
        )
        return field_, environment

    def test_there_is_one_triangulation_per_copper_layer(self, tmp_path: Path) -> None:
        field_, _ = self._field("mcu-4layer", tmp_path)
        assert set(field_.layers) == {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
        for layer in field_.layers.values():
            assert layer.triangulation.triangles

    def test_planes_are_triangulated_too_because_barrels_pass_through_them(
        self, tmp_path: Path
    ) -> None:
        """A via has to clear whatever is on a layer it only passes."""
        field_, _ = self._field("mcu-4layer", tmp_path)
        assert field_.layers["In1.Cu"].triangulation.triangles

    def test_a_cut_has_a_capacity_and_usage_is_reversible(self, tmp_path: Path) -> None:
        """Ripping up costs nothing. That is the whole reason it is a number."""
        field_, _ = self._field("routing-demo", tmp_path)
        crossings = {"F.Cu": [0, 1, 2]}
        before = list(field_.layers["F.Cu"].used)
        field_.add_usage(crossings, 0.45)
        assert field_.layers["F.Cu"].used != before
        field_.remove_usage(crossings, 0.45)
        assert field_.layers["F.Cu"].used == pytest.approx(before)

    def test_an_over_subscribed_cut_is_visible(self, tmp_path: Path) -> None:
        field_, _ = self._field("routing-demo", tmp_path)
        layer = field_.layers["F.Cu"]
        assert not field_.congested()
        layer.used[0] = layer.capacity[0] + 1.0
        assert field_.congested() == {"F.Cu": [0]}

    def test_history_only_charges_the_cuts_that_are_over(self, tmp_path: Path) -> None:
        field_, _ = self._field("routing-demo", tmp_path)
        layer = field_.layers["F.Cu"]
        layer.used[3] = layer.capacity[3] * 2
        assert field_.age(4.0) == 1
        assert layer.history[3] > 0
        assert layer.history[2] == 0

    def test_a_cut_too_narrow_for_one_track_is_a_wall_not_a_queue(
        self, tmp_path: Path
    ) -> None:
        field_, _ = self._field("usb-port", tmp_path)
        layer = field_.layers["F.Cu"]
        narrow = min(range(len(layer.capacity)), key=lambda i: layer.capacity[i])
        assert not layer.fits(narrow, layer.capacity[narrow] + 0.01)

    def test_via_sites_know_how_much_room_they_have_on_each_layer(
        self, tmp_path: Path
    ) -> None:
        field_, _ = self._field("mcu-4layer", tmp_path)
        assert field_.sites
        for site in field_.sites[:20]:
            assert site.room
            assert set(site.room) <= set(field_.layers)
            assert all(value >= 0 for value in site.room.values())

    def test_room_needed_does_not_charge_the_clearance_twice(
        self, tmp_path: Path
    ) -> None:
        """The field's obstacles are already grown; a via pays the difference."""
        field_, _ = self._field("mcu-4layer", tmp_path)
        assert field_.inflation > 0
        assert field_.room_needed(0.55) == pytest.approx(0.55 - field_.inflation)
        assert field_.room_needed(0.1) == 0.0


# ---------------------------------------------------------------------------
# routing whole boards
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestMultilayerRouting:
    def test_led_blinker_and_usb_port_now_route_completely(
        self, tmp_path: Path
    ) -> None:
        """The two boards that could not be routed on one layer at all.

        `led-blinker`'s DIP-8 and `usb-port`'s 0.65 mm-pitch receptacle are physical
        constraints, not router weaknesses: at 0.25 mm tracks and 0.2 mm clearance
        there is no corridor between those pads. The second layer is the answer, and
        this is the test that says it worked.
        """
        for name in ("led-blinker", "usb-port"):
            routed, _, _, _ = route(name, tmp_path / name)
            assert not routed.failed, [
                f"{f.key()}: {f.reason}" for f in routed.failed
            ]
            assert routed.vias, f"{name} should need at least one layer change"

    def test_the_examples_that_worked_before_still_do(self, tmp_path: Path) -> None:
        for name in ("ldo-supply", "routing-demo", "diff-pair"):
            routed, _, _, _ = route(name, tmp_path / name)
            assert not routed.failed, [
                f"{f.key()}: {f.reason}" for f in routed.failed
            ]

    def test_a_plane_layer_never_carries_a_signal(self, tmp_path: Path) -> None:
        routed, netlist, _, _ = route("mcu-4layer", tmp_path)
        planes = set(netlist.layout.stackup.plane_layers)
        assert planes == {"In1.Cu", "In2.Cu"}
        assert not [leg for leg in routed.routed if leg.layer in planes]
        assert not routed.failed

    def test_a_four_layer_board_routes_completely_and_uses_both_signal_layers(
        self, tmp_path: Path
    ) -> None:
        routed, _, _, _ = route("mcu-4layer", tmp_path)
        assert not routed.failed
        assert {leg.layer for leg in routed.routed} == {"F.Cu", "B.Cu"}
        assert routed.vias

    def test_a_via_settles_where_its_two_legs_pull_it(self, tmp_path: Path) -> None:
        """The search picks the pocket; the stretcher picks the spot inside it.

        Measured as the detour the two legs make to reach the via rather than going
        straight past it. It cannot be zero -- a via has to clear copper, and
        sometimes the straight line is where the copper is -- but it should be small,
        and it is a lot smaller than the site the search handed over.
        """
        routed, _, _, _ = route("mcu-4layer", tmp_path)
        detour = 0.0
        for connection in routed.connections:
            for index, via in enumerate(connection.vias):
                if index + 1 >= len(connection.legs):
                    continue
                before, after = connection.legs[index], connection.legs[index + 1]
                if len(before.points) < 2 or len(after.points) < 2:
                    continue
                anchor, onward = before.points[-2], after.points[1]
                detour += (
                    math.dist(anchor, via.point)
                    + math.dist(via.point, onward)
                    - math.dist(anchor, onward)
                )
        assert routed.vias
        assert detour < 8.0, f"{detour:.2f} mm of copper spent bending round vias"

    def test_a_via_is_a_column_through_the_whole_board(self, tmp_path: Path) -> None:
        """A through via on four layers spans the outers, whatever it connects."""
        routed, netlist, _, _ = route("mcu-4layer", tmp_path)
        stack = stack_for(netlist.layout)
        for via in routed.vias:
            assert via.kind == "through"
            assert set(stack.barrel_span(via.from_layer, via.to_layer)) == set(
                stack.copper
            )

    def test_a_congested_board_needs_two_layers_and_gets_them(
        self, tmp_path: Path
    ) -> None:
        """Reversing four wires in a channel is not planar. It is that simple."""
        one_layer, _, _, _ = route("congestion", tmp_path / "one", layers=("F.Cu",))
        assert one_layer.failed, "a reversal in a channel cannot be routed on one layer"

        both, _, _, _ = route("congestion", tmp_path / "both")
        assert not both.failed, [f"{f.key()}: {f.reason}" for f in both.failed]
        assert both.vias, "getting to the other layer takes a via"
        assert both.negotiation is not None
        assert both.negotiation.converged

    def test_the_negotiation_reports_what_it_did(self, tmp_path: Path) -> None:
        routed, _, _, _ = route("usb-port", tmp_path)
        assert routed.negotiation is not None
        log = routed.negotiation.log
        assert log, "every negotiation pass is logged"
        assert log[0]["iteration"] == 1
        assert set(log[0]) >= {
            "iteration",
            "present",
            "routed",
            "unrouted",
            "over_subscribed_cuts",
            "rerouted",
        }

    @pytest.mark.parametrize("loud", ("X1", "X2"))
    def test_a_protected_net_owns_the_corridor_and_the_other_detours(
        self, loud: str, tmp_path: Path
    ) -> None:
        """The brief's priority test, on a board built to make it visible.

        Two nets cross the same channel, which is wide enough for both. One is
        priority 90 and `rip_up: never`; the other is priority 10. The protected one
        takes the direct run and the other goes the long way round -- and swapping
        which is which swaps who detours, so it is the *policy* deciding and not the
        geometry.
        """
        board = _priority_probe(tmp_path, loud=loud, channel=1.6)
        routed = route_board(board.board, board.netlist, Report(), layers=("F.Cu",))
        assert not routed.failed
        crossings = {
            connection.net: connection.copper_length
            for connection in routed.connections
            if connection.copper_length > 10
        }
        quiet = "X2" if loud == "X1" else "X1"
        assert crossings[loud] < crossings[quiet] / 1.5, (
            f"{loud} is protected and should have the direct run: {crossings}"
        )

    @pytest.mark.parametrize("loud", ("X1", "X2"))
    def test_when_only_one_can_pass_the_protected_net_is_the_one(
        self, loud: str, tmp_path: Path
    ) -> None:
        """Narrow the channel to one track and the argument has a loser.

        Which is the point: the failure is reported, by name, rather than being
        resolved by whichever net the router happened to reach first.
        """
        board = _priority_probe(tmp_path, loud=loud, channel=0.8)
        routed = route_board(board.board, board.netlist, Report(), layers=("F.Cu",))
        quiet = "X2" if loud == "X1" else "X1"
        assert [f.net for f in routed.failed] == [quiet]
        assert any(
            connection.net == loud and connection.copper_length > 10
            for connection in routed.connections
        )

    def test_input_order_does_not_decide_the_outcome(self, tmp_path: Path) -> None:
        """Shuffled input converges to *a* valid result, not necessarily the same one.

        The point of negotiating rather than ordering is that the answer stops
        depending on a guess made before anything was known. What must not change is
        that the board routes and that the corridors balance.
        """
        from aipcb.netlist import Netlist

        report = Report()
        result = build_design(design("congestion"), out_dir=tmp_path, report=report)
        text = next(
            p for p in result.written if p.suffix == ".kicad_pcb"
        ).read_text(encoding="utf-8")

        outcomes = []
        for order in (sorted(result.netlist.nets), sorted(result.netlist.nets, reverse=True)):
            shuffled = Netlist(
                name=result.netlist.name,
                revision=result.netlist.revision,
                components=result.netlist.components,
                nets={name: result.netlist.nets[name] for name in order},
                constraints=result.netlist.constraints,
                net_classes=result.netlist.net_classes,
                layout=result.netlist.layout,
            )
            outcomes.append(route_board(parse(text), shuffled, Report()))

        for outcome in outcomes:
            assert not outcome.failed
            assert outcome.negotiation is not None
            assert outcome.negotiation.converged

    def test_the_same_source_gives_the_same_board(self, tmp_path: Path) -> None:
        """Determinism is separate from order-independence, and also required."""
        first, _, _, _ = route("mcu-4layer", tmp_path / "a")
        second, _, _, _ = route("mcu-4layer", tmp_path / "b")
        assert [leg.points for leg in first.routed] == [
            leg.points for leg in second.routed
        ]
        assert [v.point for v in first.vias] == [v.point for v in second.vias]

    def test_manual_copper_is_routed_around_not_through(self, tmp_path: Path) -> None:
        """M6 preserves a hand-routed track; M8 has to treat it as a wall."""
        from shapely.geometry import LineString
        from shapely.geometry import Polygon as ShapelyPolygon

        from aipcb.route.obstacles import preserved_copper

        report = Report()
        result = build_design(design("routing-demo"), out_dir=tmp_path, report=report)
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        board = parse(board_path.read_text(encoding="utf-8"))

        hand_drawn = parse(
            '(segment (start 110 118) (end 150 118) (width 1.0) (layer "F.Cu") '
            '(net 0) (uuid "3f2c1a44-0000-4000-8000-000000000001"))'
        )
        board.add(hand_drawn)
        assert len(preserved_copper(board)) == 1

        routed = route_board(board, result.netlist, report)
        wall = LineString([(110, 118), (150, 118)]).buffer(0.5 + 0.2)
        for leg in routed.routed:
            if len(leg.points) < 2 or leg.layer != "F.Cu":
                continue
            overlap = ShapelyPolygon(wall).intersection(LineString(leg.points))
            assert overlap.length < 1e-6, f"{leg.net} crossed the manual track"


# ---------------------------------------------------------------------------
# emitting vias
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestEmission:
    def test_vias_reach_the_board(self, tmp_path: Path) -> None:
        routed, netlist, board, _ = route("usb-port", tmp_path)
        _segments, vias = attach_copper(
            board, routed.connections, sorted(netlist.nets)
        )
        assert vias == len(routed.vias) > 0
        emitted = list(board.children("via"))
        assert len(emitted) == vias
        for node in emitted:
            assert node.child("at") is not None
            assert node.child("size") is not None
            assert node.child("drill") is not None
            assert len(list(node.child("layers").atoms())) == 2

    def test_via_uuids_are_derived_not_random(self) -> None:
        assert via_uuid("GND", "n1", "through") == via_uuid("GND", "n1", "through")
        assert via_uuid("GND", "n1", "through") != via_uuid("GND", "n2", "through")

    def test_our_own_copper_can_be_told_from_a_human_s(self, tmp_path: Path) -> None:
        """Re-running the router replaces its own output and keeps everything else."""
        routed, netlist, board, _ = route("usb-port", tmp_path)
        attach_copper(board, routed.connections, sorted(netlist.nets))
        hand_drawn = parse(
            '(segment (start 110 130) (end 120 130) (width 0.5) (layer "F.Cu") '
            '(net 0) (uuid "3f2c1a44-0000-4000-8000-000000000002"))'
        )
        board.add(hand_drawn)

        removed = drop_generated(board, generated_uuids(routed.connections))
        assert removed > 0
        left = list(board.children("segment")) + list(board.children("via"))
        assert [item.get("uuid") for item in left] == [
            "3f2c1a44-0000-4000-8000-000000000002"
        ]


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


@needs_kicad_libraries
@needs_kicad_cli
class TestAcceptance:
    def _drc(self, name: str, out: Path) -> dict:
        from aipcb.kicad.cli import run_kicad

        report = Report()
        result = build_design(design(name), out_dir=out, report=report)
        board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
        board = parse(board_path.read_text(encoding="utf-8"))
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        routed = route_board(board, result.netlist, report, topologies=topologies)
        attach_copper(board, routed.connections, sorted(result.netlist.nets))
        board_path.write_text(dump(board), encoding="utf-8")

        report_path = out / "drc.json"
        run = run_kicad(
            "pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
            "-o", str(report_path), str(board_path),
        )
        assert run.returncode == 0, run.stdout + run.stderr
        return json.loads(report_path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("name", ("mcu-4layer", "congestion"))
    def test_the_new_examples_are_drc_clean(self, name: str, tmp_path: Path) -> None:
        drc = self._drc(name, tmp_path)
        assert not drc["violations"], [
            f"{v['type']}: {v['description']}" for v in drc["violations"]
        ]
        assert not drc["unconnected_items"]
        assert not drc["schematic_parity"]

    def test_a_four_layer_board_is_byte_stable(self, tmp_path: Path) -> None:
        boards = []
        for run in ("a", "b"):
            target = tmp_path / run
            report = Report()
            result = build_design(design("mcu-4layer"), out_dir=target, report=report)
            path = next(p for p in result.written if p.suffix == ".kicad_pcb")
            board = parse(path.read_text(encoding="utf-8"))
            routed = route_board(board, result.netlist, report)
            attach_copper(board, routed.connections, sorted(result.netlist.nets))
            boards.append(dump(board))
        assert boards[0] == boards[1]

    def test_the_pairs_on_usb_port_couple(self, tmp_path: Path) -> None:
        """M7 could couple neither. M8 couples the one that is a pair."""
        routed, _, _, report = route("usb-port", tmp_path)
        assert [pair.key() for pair in routed.pairs] == ["USB_DM+USB_DP"]
        coupled = [d for d in report if d.code == "diff-pair-coupled"]
        assert coupled and all(d.message for d in coupled)

    def test_a_coupled_pair_meets_its_skew_budget_after_meandering(
        self, tmp_path: Path
    ) -> None:
        routed, _netlist, _, report = route("usb-port", tmp_path)
        pair = routed.pairs[0]
        assert pair.max_skew is not None
        assert routed.skew[pair.key()] <= pair.max_skew
        assert [d for d in report if d.code == "diff-pair-length-matched"], (
            "the pair needed meandering to get there"
        )

    def test_both_pairs_on_the_four_layer_board_couple(self, tmp_path: Path) -> None:
        routed, _, _, _ = route("mcu-4layer", tmp_path)
        assert sorted(pair.key() for pair in routed.pairs) == [
            "DEV_DM+DEV_DP",
            "USB_DM+USB_DP",
        ]


#: Three nets, one channel that will not hold them, and the widest one pinned in
#: place by `rip_up: never`. Built to make the failure report say something useful.
_NEVER_PROBE = """
name: never-probe
revision: A
libraries:
  - {library}/connectors.yaml
net_classes:
  loud:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    priority: 95
    rip_up: never
  quiet:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    priority: 10
nets:
  X1: {{ class: loud }}
  X2: {{ class: quiet }}
  X3: {{ class: quiet }}
components:
  J1:
    part: CONN_ISP_1X06
    role: connector
    pins: {{ MISO: X1, VCC: X2, SCK: X3, MOSI: X1, RESET: X2, GND: X3 }}
  J2:
    part: CONN_ISP_1X06
    role: connector
    pins: {{ MISO: X3, VCC: X2, SCK: X1, MOSI: X3, RESET: X2, GND: X1 }}
layout:
  outline: {{ shape: rect, width_mm: 30.0, height_mm: 24.0 }}
  stackup: {{ copper_layers: 2, thickness_mm: 1.6 }}
  placement:
    margin_mm: 2.0
    keepouts:
      - region_mm: [10, 0, 20, 11.3]
      - region_mm: [10, 12.7, 20, 24]
    rules:
      - members: [J1]
        region_mm: [2, 3, 8.2, 21]
      - members: [J2]
        region_mm: [21, 3, 27.2, 21]
"""


@needs_kicad_libraries
class TestSketchedViaHops:
    def test_a_hand_written_via_hop_becomes_two_legs_and_a_via(
        self, tmp_path: Path
    ) -> None:
        """M7 modelled via hops and refused to build them. This is the difference.

        Nothing in the sketch says where the via goes -- that is the whole point of
        storing topology -- so the position is derived: between the things on either
        side of it, slid along that line until it fits on every layer its barrel
        passes through.
        """
        from aipcb.route.check import _layer_geometry
        from aipcb.route.model import RouteTopology
        from aipcb.route.obstacles import extract_obstacles
        from aipcb.route.plan import rules_for
        from aipcb.route.stretch import stretch_route

        result = build_design(design("routing-demo"), out_dir=tmp_path, report=Report())
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        environment = extract_obstacles(board)
        stack = stack_for(result.netlist.layout)
        route = RouteTopology.model_validate(
            {
                "net": "CROSS",
                "from": "J1.1",
                "to": "J2.1",
                "layer": "F.Cu",
                "passes": [
                    {"obstacle": "U1.2", "side": "left"},
                    {"kind": "via", "to_layer": "B.Cu", "name": "hop"},
                ],
            }
        )
        rules = rules_for(result.netlist, "CROSS")
        built = stretch_route(
            route,
            environment,
            _layer_geometry(environment, result.netlist, route, rules),
            rules,
            stack=stack,
        )

        assert [leg.layer for leg in built.legs] == ["F.Cu", "B.Cu"]
        assert built.legs[0].end == "via:hop" == built.legs[1].start
        assert len(built.vias) == 1
        via = built.vias[0]
        assert (via.from_layer, via.to_layer, via.kind) == ("F.Cu", "B.Cu", "through")
        # The legs meet at the via, exactly, or KiCad reports a dangling track.
        assert built.legs[0].points[-1] == via.point == built.legs[1].points[0]
        # And the barrel is conductor, so it counts towards the route's length.
        assert built.barrel_length > 1.0
        assert built.length > built.copper_length


@needs_kicad_libraries
class TestFailureReporting:
    def test_a_board_that_will_not_settle_names_the_net_holding_it(
        self, tmp_path: Path
    ) -> None:
        """`never` is a promise the router keeps and then explains.

        Three nets want a channel that holds two, and the one that may not be ripped
        up is the reason the other two cannot both fit. Reporting "congested" without
        saying who is sitting in the corridor leaves the reader nowhere to go.
        """
        path = tmp_path / "never"
        path.mkdir()
        (path / "design.yaml").write_text(
            _NEVER_PROBE.format(
                library=(REPO_ROOT / "examples" / "library").resolve()
            ),
            encoding="utf-8",
        )
        result = build_design(path / "design.yaml", out_dir=path, report=Report())
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        report = Report()
        routed = route_board(board, result.netlist, report, layers=("F.Cu",))

        assert routed.negotiation is not None
        assert not routed.negotiation.converged
        assert routed.negotiation.blocked_by == ["X1 (never)"]

        congested = [d for d in report if d.code == "routing-congested"]
        assert congested, "a board that did not settle should say so"
        assert "X1 (never)" in (congested[0].hint or "")

    def test_a_board_that_settles_says_nothing_about_congestion(
        self, tmp_path: Path
    ) -> None:
        routed, _, _, report = route("ldo-supply", tmp_path)
        assert routed.negotiation is not None
        assert routed.negotiation.converged
        assert not [d for d in report if d.code == "routing-congested"]


@needs_kicad_libraries
class TestRouteCheck:
    def test_cuts_that_cannot_hold_the_declared_routes_are_reported(
        self, tmp_path: Path
    ) -> None:
        """Two sketches, each realizable, that cannot both be built.

        This is the question the per-route check cannot answer: a route can be
        perfectly sound on its own and impossible alongside the one next to it, and
        the only way to see that is to add the corridors up.
        """
        from aipcb.route.check import check_routes

        source = _PRIORITY_PROBE.format(
            library=(REPO_ROOT / "examples" / "library").resolve(),
            first="loud",
            second="quiet",
            top=9.6,
            bottom=10.4,
        ).replace(
            "layout:\n",
            "layout:\n  routes:\n"
            "    - {net: X1, from: J1.1, to: J2.2, layer: F.Cu}\n"
            "    - {net: X2, from: J1.2, to: J2.1, layer: F.Cu}\n",
        )
        path = tmp_path / "over"
        path.mkdir()
        (path / "design.yaml").write_text(source, encoding="utf-8")

        report = Report()
        result = build_design(path / "design.yaml", out_dir=path, report=report)
        board = parse(
            next(p for p in result.written if p.suffix == ".kicad_pcb").read_text(
                encoding="utf-8"
            )
        )
        outcome = check_routes(board, result.netlist, Report())
        assert outcome.over_subscribed, "two tracks cannot share a one-track channel"
        first = outcome.over_subscribed[0]
        assert first["demand_mm"] > first["width_mm"]
        assert set(first["nets"]) <= {"X1", "X2"}
        assert not outcome.ok


@needs_kicad_libraries
class TestCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "route", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_routing_twice_replaces_its_own_copper(self, tmp_path: Path) -> None:
        """Not duplicates it. The second run must produce the same file, byte for byte."""
        target = str(tmp_path)
        source = str(design("led-blinker"))
        board = tmp_path / "led-blinker.kicad_pcb"

        self._run("all", source, "--out", target)
        first = board.read_text(encoding="utf-8")
        self._run("all", source, "--out", target)
        assert board.read_text(encoding="utf-8") == first

    def test_layers_can_be_restricted_and_it_shows(self, tmp_path: Path) -> None:
        """`--layers F.Cu` is how you ask whether one layer would have done."""
        source = str(design("congestion"))
        both = json.loads(
            self._run("all", source, "--out", str(tmp_path / "both"), "--json").stdout
        )
        assert both["routing"]["failed"] == 0
        assert both["routing"]["vias"] > 0

        one = json.loads(
            self._run(
                "all", source, "--out", str(tmp_path / "one"),
                "--layers", "F.Cu", "--json",
            ).stdout
        )
        assert one["routing"]["failed"] > 0
        assert one["routing"]["layers"] == ["F.Cu"]


# ---------------------------------------------------------------------------
# cost model
# ---------------------------------------------------------------------------


class TestCosts:
    def test_every_term_is_named_and_documented(self) -> None:
        """docs/routing-costs.md is the contract; this is the part that can be tested."""
        text = (REPO_ROOT / "docs" / "routing-costs.md").read_text(encoding="utf-8")
        for field in CostModel.__dataclass_fields__:
            assert field in text, f"{field} is not documented in routing-costs.md"

    def test_a_via_costs_what_the_brief_asked_for(self) -> None:
        assert 3.0 <= DEFAULT_COSTS.via_cost_mm <= 10.0

    def test_a_plane_is_infinite_and_a_full_cut_is_not(self) -> None:
        assert DEFAULT_COSTS.plane_layer_mm == math.inf
        assert math.isfinite(DEFAULT_COSTS.congestion_cap)
        assert math.isfinite(DEFAULT_COSTS.rip_up_never)

    def test_the_congestion_schedule_rises(self) -> None:
        assert DEFAULT_COSTS.congestion_growth > 1.0

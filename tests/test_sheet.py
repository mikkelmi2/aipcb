"""Readable schematics: the placement, the wiring, and the policy on editing them.

M2's bar was "correctness over beauty" and M14's is "reviewable by convention". The
tests here are about the second bar, and they are deliberately about *properties*
rather than coordinates: a test that asserts C1 sits at (138.43, 156.21) would fail
on every improvement and prove nothing about readability.

The properties are the ones the milestone names. Flow runs left to right. A
decoupling capacitor is near the IC it is declared ``for:``. Rails point up and
grounds point down. Nothing overlaps anything. And -- the one that matters most --
none of it changes a single net, which is checked against KiCad's own netlister
rather than asserted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.geometry import place_direction
from aipcb.compile.readability import measure_schematic, read_symbols
from aipcb.compile.review import decoupling_hosts
from aipcb.compile.schematic import (
    GROUND_LIB_ID,
    RAIL_LIB_ID,
    _resolve_symbols,
    plan_sheet,
    power_symbol_for,
    undriven_power_nets,
)
from aipcb.compile.sheet import SheetPlan, dense_sides
from aipcb.diagnostics import Report
from aipcb.kicad.cli import run_kicad
from aipcb.kicad.sexpr import parse
from aipcb.kicad.symbols import resolve_symbol

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

EXAMPLES = REPO_ROOT / "examples"


def plan_for(name: str) -> tuple[SheetPlan, object]:
    netlist = compile_netlist(EXAMPLES / name / "design.yaml", Report())
    symbols = _resolve_symbols(netlist, extra=())
    flags = undriven_power_nets(netlist, symbols)
    return plan_sheet(netlist, symbols, flags), netlist


def sheet_for(name: str, tmp_path: Path) -> Path:
    result = build_design(EXAMPLES / name / "design.yaml", out_dir=tmp_path, report=Report())
    return next(p for p in result.written if p.suffix == ".kicad_sch")


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestSignalFlow:
    def test_the_input_connector_is_left_of_the_controller(self) -> None:
        """pcie-sata is the case the ranking exists for.

        The card takes a PCIe lane in at its edge fingers and gives four SATA ports
        out. Seeding the flow from *every* connector -- which is what "connectors on
        the left" reads like at first -- puts a SATA port in the same column as the
        edge connector it is downstream of, and the sheet then says the opposite of
        what the board does.
        """
        plan, _ = plan_for("pcie-sata")
        edge = plan.placements["J1"].origin.x
        controller = plan.placements["U1"].origin.x
        ports = [plan.placements[r].origin.x for r in ("J2", "J3", "J4", "J5")]
        assert edge < controller, "the PCIe edge connector belongs left of the controller"
        assert all(controller < port for port in ports), (
            "the SATA ports are what the controller drives; they belong to its right"
        )

    def test_flow_ranks_are_a_left_to_right_order(self) -> None:
        plan, _ = plan_for("pcie-sata")
        by_rank: dict[int, list[float]] = {}
        for block in plan.blocks:
            by_rank.setdefault(block.rank, []).append(block.origin.x)
        columns = [min(xs) for _, xs in sorted(by_rank.items())]
        assert columns == sorted(columns), "a higher rank must never sit further left"

    def test_a_module_instance_becomes_one_cluster(self) -> None:
        """The module hierarchy is structure the source declares; the sheet shows it."""
        plan, _ = plan_for("led-blinker")
        module = next(b for b in plan.blocks if b.key == "module:led1")
        assert set(module.members) == {"D1", "R2"}
        assert module.label == "led1"
        # And its members are together rather than scattered across the sheet.
        xs = [plan.placements[r].origin.x for r in module.members]
        ys = [plan.placements[r].origin.y for r in module.members]
        assert max(xs) - min(xs) < 40.0
        assert max(ys) - min(ys) < 60.0


# ---------------------------------------------------------------------------
# role-driven adjacency
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestRoleAdjacency:
    @pytest.mark.parametrize(
        "name", ["pcie-sata", "mcu-4layer", "qfn-fanout", "ldo-supply", "usb-port"]
    )
    def test_decoupling_sits_at_the_part_it_serves(self, name: str, tmp_path: Path) -> None:
        """The number M14 exists to move, measured the way the report measures it."""
        netlist = compile_netlist(EXAMPLES / name / "design.yaml", Report())
        root = parse(sheet_for(name, tmp_path).read_text(encoding="utf-8"))
        metrics = measure_schematic(root, decoupling_hosts(netlist))
        assert metrics.decoupling_pairs > 0, "this example should have local capacitors"
        assert metrics.decoupling_max_mm < 45.0, (
            f"{name}: the furthest local capacitor is {metrics.decoupling_max_mm:.1f} mm "
            "from the pin it serves"
        )

    def test_an_unhosted_pull_up_still_finds_its_net(self) -> None:
        """A pull-up with no `for:` belongs beside whatever it holds high."""
        from aipcb.compile.sheet import POWER_CLASSES, _satellites

        netlist = compile_netlist(EXAMPLES / "routing-demo" / "design.yaml", Report())
        power = frozenset(
            n.name for n in netlist.sorted_nets() if n.net_class in POWER_CLASSES
        )
        hosts = _satellites(netlist, power)
        pull_ups = [
            c.refdes
            for c in netlist.sorted_components()
            if (c.role or "") in ("pull_up", "pull_down")
        ]
        assert pull_ups, "routing-demo declares a pull-up"
        for refdes in pull_ups:
            assert refdes in hosts, f"{refdes} was not attached to anything"


# ---------------------------------------------------------------------------
# power convention
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestPowerConvention:
    def test_rails_point_up_and_grounds_point_down(self, tmp_path: Path) -> None:
        """The convention that makes a sheet skimmable, checked as geometry.

        A KiCad ground symbol's pin points up out of a body that hangs below it, and
        a rail symbol's is the mirror of that. Placing both unrotated is what puts
        every ground at the bottom of its stub and every rail at the top -- so the
        assertion is simply that none of them is turned.
        """
        root = parse(sheet_for("mcu-4layer", tmp_path).read_text(encoding="utf-8"))
        power = [s for s in read_symbols(root) if s.refdes.startswith("#PWR")]
        assert power, "the sheet should carry power symbols"
        assert all(s.rotation == 0.0 for s in power)

    def test_a_net_named_like_a_stock_symbol_gets_that_symbol(self) -> None:
        assert power_symbol_for("GND", ground=True) == "power:GND"
        assert power_symbol_for("VCC", ground=False) == "power:VCC"
        # And one that has no stock symbol gets the generic shape with its own name.
        assert power_symbol_for("P3V3", ground=False) == RAIL_LIB_ID
        assert power_symbol_for("DGND_A", ground=True) == GROUND_LIB_ID

    def test_a_crowded_pin_row_gets_labels_rather_than_symbols(self) -> None:
        """A ground symbol is wider than a 2.54 mm pin pitch; a label is not.

        Standing one on every pin of a six-way header puts three net names on top of
        three others, which is worse than the label it replaced.
        """
        header = resolve_symbol("Connector_Generic:Conn_01x06")
        dense = dense_sides(header, 0.0)
        assert dense, "a 2.54 mm header is a crowded side"
        capacitor = resolve_symbol("Device:C")
        assert not dense_sides(capacitor, 0.0), "a two-pin part has room for symbols"

    def test_a_polarised_pair_stands_with_its_rail_on_top(self) -> None:
        plan, netlist = plan_for("mcu-4layer")
        caps = [
            c for c in netlist.sorted_components()
            if (c.role or "") in ("decoupling", "bulk", "bypass")
        ]
        assert caps
        for cap in caps:
            symbol = resolve_symbol(cap.part.symbol)
            rotation = plan.placements[cap.refdes].rotation
            rail_pins = [
                p for p in symbol.pins
                if (net := cap.connections.get(p.number))
                and netlist.nets[net].net_class == "power"
            ]
            if not rail_pins:
                continue
            outward = place_direction(rotation, rail_pins[0].outward_angle)
            assert outward.y < 0, (
                f"{cap.refdes}: its rail pin points down, so the rail symbol would "
                "hang below the capacitor"
            )


# ---------------------------------------------------------------------------
# nothing lands on anything
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestNothingOverlaps:
    @pytest.mark.parametrize(
        "name",
        [
            "congestion", "diff-pair", "enclosure", "ldo-supply", "led-blinker",
            "mcu-4layer", "overconstrained", "pcie-sata", "qfn-fanout",
            "routing-demo", "usb-port",
        ],
    )
    def test_no_two_drawn_things_collide(self, name: str, tmp_path: Path) -> None:
        netlist = compile_netlist(EXAMPLES / name / "design.yaml", Report())
        root = parse(sheet_for(name, tmp_path).read_text(encoding="utf-8"))
        metrics = measure_schematic(root, decoupling_hosts(netlist))
        assert metrics.collisions == 0, f"{name}: {metrics.collisions} overlapping items"

    def test_no_wire_runs_through_a_symbol(self, example_design: Path, tmp_path: Path) -> None:
        result = build_design(example_design, out_dir=tmp_path, report=Report())
        sheet = next(p for p in result.written if p.suffix == ".kicad_sch")
        metrics = measure_schematic(parse(sheet.read_text(encoding="utf-8")))
        assert metrics.wire_through_symbol == 0
        assert metrics.wire_crossings == 0

    def test_stacked_pins_are_drawn_once(self, tmp_path: Path) -> None:
        """KiCad's PCIe edge symbol stacks nine ground pins on one coordinate.

        Drawing a stub and a label for each produces nine identical labels in one
        place. One point carrying one net is one connection, and gets drawn once --
        which is also exactly why the library draws the pins that way.
        """
        root = parse(sheet_for("pcie-sata", tmp_path).read_text(encoding="utf-8"))
        anchors = [
            (w.child("pts").children("xy").__next__().value(0),
             w.child("pts").children("xy").__next__().value(1))
            for w in root.children("wire")
            if w.child("pts") is not None
        ]
        assert len(anchors) == len(set(anchors)), "two stubs start at the same pin"


# ---------------------------------------------------------------------------
# the guardrail: nothing electrical moved
# ---------------------------------------------------------------------------


@needs_kicad_cli
@needs_kicad_libraries
class TestNothingElectricalMoved:
    def test_kicad_extracts_the_same_netlist_as_the_source(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """The whole of M14 is a presentation change, and this is what says so.

        Power symbols replaced net labels on every rail, which is a real change to
        what is on the sheet -- so the netlist KiCad extracts is compared name for
        name as well as pin for pin. A power symbol names its net after its value,
        and if that were ever untrue this is where it would show.
        """
        result = build_design(example_design, out_dir=tmp_path, report=Report())
        schematic = next(p for p in result.written if p.suffix == ".kicad_sch")
        netlist = compile_netlist(example_design, Report())

        xml_path = tmp_path / "net.xml"
        run = run_kicad(
            "sch", "export", "netlist", "--format", "kicadxml",
            "-o", str(xml_path), str(schematic),
        )
        assert run.returncode == 0, run.stderr

        from_kicad: dict[str, set[str]] = {}
        for net in ET.parse(xml_path).getroot().find("nets"):  # type: ignore[union-attr]
            members = {
                f"{node.get('ref')}.{node.get('pin')}"
                for node in net.findall("node")
                if not str(node.get("ref")).startswith("#")
            }
            if len(members) > 1:
                from_kicad[str(net.get("name"))] = members

        from_source = {
            net.name: {f"{node.refdes}.{node.pin}" for node in net.nodes}
            for net in netlist.sorted_nets()
            if net.degree > 1
        }
        assert from_kicad == from_source

"""Flattening hierarchical designs: modules, parameters, and reference designators."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aipcb.diagnostics import Report
from aipcb.elaborate import elaborate
from aipcb.loader import load_design
from aipcb.netlist import Netlist

LIB = """
parts:
  R:
    symbol: Device:R
    footprint: Resistor_SMD:R_0603_1608Metric
    pins: { "1": {}, "2": {} }
  R2:
    symbol: Device:R
    footprint: Resistor_SMD:R_0603_1608Metric
    pins: { "1": {}, "2": {} }
  C:
    symbol: Device:C
    footprint: Capacitor_SMD:C_0603_1608Metric
    pins: { "1": {}, "2": {} }
"""


@pytest.fixture
def build(write_design: Callable[..., Path], tmp_path: Path):
    (tmp_path / "lib.yaml").write_text(LIB, encoding="utf-8")

    def _build(text: str) -> tuple[Netlist, Report]:
        report = Report()
        loaded = load_design(write_design(text), report=report)
        return elaborate(loaded, report), report

    return _build


FLAT = """
name: t
libraries: [lib.yaml]
components:
  R1: { part: R, pins: { "1": A, "2": B } }
  C1: { part: C, pins: { "1": B, "2": A } }
"""


class TestFlatDesigns:
    def test_component_keys_become_reference_designators(self, build) -> None:
        netlist, report = build(FLAT)
        assert report.ok
        assert set(netlist.components) == {"R1", "C1"}

    def test_nets_collect_their_nodes(self, build) -> None:
        netlist, _ = build(FLAT)
        assert {str(n) for n in netlist.nets["A"].nodes} == {"R1.1", "C1.2"}

    def test_pins_resolve_by_functional_name(self, build, tmp_path: Path) -> None:
        (tmp_path / "lib.yaml").write_text(
            "parts:\n  D:\n    symbol: Device:LED\n"
            "    footprint: LED_SMD:LED_0603_1608Metric\n"
            '    pins: { "1": { name: K }, "2": { name: A } }\n',
            encoding="utf-8",
        )
        netlist, report = build(
            "name: t\nlibraries: [lib.yaml]\ncomponents:\n"
            "  D1: { part: D, pins: { A: NET1, K: NET2 } }\n"
            "  D2: { part: D, pins: { A: NET2, K: NET1 } }\n"
        )
        assert report.ok
        assert netlist.components["D1"].connections == {"2": "NET1", "1": "NET2"}


MODULE = """
name: t
libraries: [lib.yaml]
nets:
  VCC: { class: power }
  GND: { class: ground }
modules:
  rail:
    params:
      cap: { type: part, default: C }
      n: { type: int, default: 2 }
    ports: [VIN, GND]
    nets:
      MID: { class: signal }
    components:
      RS: { part: R, pins: { "1": VIN, "2": MID } }
      CB: { part: "{{ cap }}", count: "{{ n }}", pins: { "1": MID, "2": GND } }
instances:
  a: { module: rail, connect: { VIN: VCC, GND: GND } }
  b: { module: rail, params: { n: 1 }, connect: { VIN: VCC, GND: GND } }
"""


class TestModules:
    def test_instances_are_expanded(self, build) -> None:
        netlist, report = build(MODULE)
        assert report.ok, report.render()
        # a: RS + 2 caps, b: RS + 1 cap
        assert len(netlist.components) == 5

    def test_local_nets_are_scoped_per_instance(self, build) -> None:
        netlist, _ = build(MODULE)
        assert "a.MID" in netlist.nets
        assert "b.MID" in netlist.nets
        assert netlist.nets["a.MID"].degree == 3  # one resistor, two capacitors

    def test_ports_bind_to_the_parent_net(self, build) -> None:
        netlist, _ = build(MODULE)
        refdes = {str(n) for n in netlist.nets["VCC"].nodes}
        assert len(refdes) == 2  # one resistor from each instance

    def test_count_is_parameterised(self, build) -> None:
        netlist, _ = build(MODULE)
        instances = netlist.module_instances()
        assert len(instances["a"]) == 3
        assert len(instances["b"]) == 2

    def test_refdes_are_assigned_deterministically(self, build) -> None:
        first, _ = build(MODULE)
        second, _ = build(MODULE)
        assert {c.path_text: c.refdes for c in first.components.values()} == {
            c.path_text: c.refdes for c in second.components.values()
        }

    def test_explicit_top_level_refdes_is_not_reused_by_a_module(self, build) -> None:
        netlist, report = build(
            MODULE.replace(
                "instances:",
                'components:\n  R1: { part: R, pins: { "1": VCC, "2": GND } }\n'
                "instances:",
            )
        )
        assert report.ok, report.render()
        assert netlist.components["R1"].path_text == "R1"
        module_resistors = [
            c for c in netlist.components.values() if c.hier[-1] == "RS"
        ]
        assert all(c.refdes != "R1" for c in module_resistors)


class TestModuleErrors:
    def test_unknown_module(self, build) -> None:
        _, report = build(
            "name: t\nlibraries: [lib.yaml]\ncomponents:\n"
            '  R1: { part: R, pins: { "1": A, "2": B } }\n'
            "instances:\n  x: { module: nope }\n"
        )
        diag = next(d for d in report if d.code == "unknown-module")
        assert "nope" in diag.message

    def test_unconnected_port(self, build) -> None:
        _, report = build(MODULE.replace("a: { module: rail, connect: { VIN: VCC, GND: GND } }",
                                         "a: { module: rail, connect: { VIN: VCC } }"))
        diag = next(d for d in report if d.code == "unconnected-port")
        assert "GND" in diag.message
        assert "under `connect:`" in (diag.hint or "")

    def test_unknown_port(self, build) -> None:
        _, report = build(
            MODULE.replace("connect: { VIN: VCC, GND: GND } }",
                           "connect: { VIN: VCC, GND: GND, NOPE: VCC } }", 1)
        )
        assert any(d.code == "unknown-port" for d in report)

    def test_unknown_parameter(self, build) -> None:
        _, report = build(
            MODULE.replace("params: { n: 1 }", "params: { n: 1, wat: 3 }")
        )
        diag = next(d for d in report if d.code == "unknown-param")
        assert "wat" in diag.message

    def test_missing_required_parameter(self, build) -> None:
        _, report = build(
            MODULE.replace("cap: { type: part, default: C }",
                           "cap: { type: part, required: true }")
        )
        assert any(d.code == "missing-param" for d in report)

    def test_recursion_is_bounded(self, build) -> None:
        _, report = build(
            "name: t\nlibraries: [lib.yaml]\nmodules:\n"
            "  loop:\n    ports: []\n    components:\n"
            '      R: { part: R, pins: { "1": A, "2": B } }\n'
            "    instances:\n      inner: { module: loop }\n"
            "instances:\n  top: { module: loop }\n"
        )
        assert any(d.code == "module-recursion" for d in report)


class TestPartResolution:
    def test_unknown_part_suggests_a_near_match(self, build) -> None:
        _, report = build(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n  U1: { part: RR, pins: { "1": A } }\n'
        )
        diag = next(d for d in report if d.code == "unknown-part")
        assert "did you mean" in (diag.hint or "")

    def test_unknown_pin_lists_the_real_ones(self, build) -> None:
        _, report = build(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n  R1: { part: R, pins: { "9": A } }\n'
        )
        diag = next(d for d in report if d.code == "unknown-pin")
        assert "pins of 'R'" in (diag.hint or "")

    def test_a_pin_cannot_be_on_two_nets(self, build, tmp_path: Path) -> None:
        (tmp_path / "lib.yaml").write_text(
            "parts:\n  D:\n    symbol: Device:LED\n"
            "    footprint: LED_SMD:LED_0603_1608Metric\n"
            '    pins: { "1": { name: K }, "2": { name: A } }\n',
            encoding="utf-8",
        )
        _, report = build(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  D1: { part: D, pins: { "1": NET1, K: NET2 } }\n'
        )
        assert any(d.code == "duplicate-pin" for d in report)

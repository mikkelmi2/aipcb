# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The query layer: reading part of a design without loading all of it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aipcb.compile.build import compile_netlist
from aipcb.diagnostics import Report
from aipcb.query import (
    components_by_role,
    describe_component,
    describe_module,
    describe_net,
    list_modules,
    nets_of_class,
    summarise_design,
)

from .conftest import REPO_ROOT, needs_kicad_libraries


@pytest.fixture
def netlist(example_design: Path):
    return compile_netlist(example_design, Report())


def load(name: str):
    return compile_netlist(REPO_ROOT / "examples" / name / "design.yaml", Report())


class TestSummary:
    def test_covers_every_component(self, netlist) -> None:
        data = summarise_design(netlist)
        assert sum(b["components"] for b in data["blocks"]) == len(netlist.components)

    def test_groups_by_module_instance(self) -> None:
        data = summarise_design(load("ldo-supply"))
        blocks = {b["block"] for b in data["blocks"]}
        assert blocks == {"(top level)", "rail_3v3"}

    def test_reports_constraints_with_their_reasons(self) -> None:
        data = summarise_design(load("usb-port"))
        kinds = {c["kind"] for c in data["constraints"]}
        assert {"group", "max_distance", "keep_apart"} <= kinds
        assert all(c["reason"] for c in data["constraints"])

    def test_stays_small(self, netlist) -> None:
        """A summary that costs as much as the source defeats its purpose."""
        data = summarise_design(netlist)
        source = (REPO_ROOT / "examples" / netlist.name / "design.yaml").read_text()
        assert len(json.dumps(data)) < len(source) * 1.2

    def test_is_json_serialisable(self, netlist) -> None:
        json.dumps(summarise_design(netlist))


class TestModules:
    def test_lists_instances(self) -> None:
        assert list_modules(load("ldo-supply")) == ["rail_3v3"]

    def test_describes_members_and_ports(self) -> None:
        data = describe_module(load("ldo-supply"), "rail_3v3")
        assert {c["refdes"] for c in data["components"]} == {"C1", "C2", "C3", "C4", "U1"}
        assert {p["net"] for p in data["ports"]} == {"VIN", "VOUT", "GND"}

    def test_includes_neighbours_across_the_boundary(self) -> None:
        """A module read in isolation says nothing about what it drives."""
        data = describe_module(load("ldo-supply"), "rail_3v3")
        assert {c["refdes"] for c in data["neighbours"]} == {"J1", "J2"}

    def test_ports_say_what_is_inside_and_outside(self) -> None:
        data = describe_module(load("ldo-supply"), "rail_3v3")
        vin = next(p for p in data["ports"] if p["net"] == "VIN")
        assert "U1.3" in vin["inside"]
        assert vin["outside"] == ["J1.1"]

    def test_unknown_module_raises(self) -> None:
        with pytest.raises(KeyError):
            describe_module(load("ldo-supply"), "nope")


class TestComponents:
    def test_describes_connections_and_neighbours(self) -> None:
        data = describe_component(load("usb-port"), "R1")
        assert data["part"] == "R_22R_0603"
        assert data["role"] == "series"
        nets = {c["net"] for c in data["connections"]}
        assert nets == {"USB_DP", "DEV_DP"}
        assert set(data["neighbours"]) == {"J1", "J2"}

    def test_carries_the_intent(self) -> None:
        data = describe_component(load("usb-port"), "R3")
        assert "floating ID pin" in data["reason"]
        assert data["for"] == "J1"

    def test_reports_what_a_part_serves(self) -> None:
        """The inverse of `for:` -- what depends on this component."""
        data = describe_component(load("usb-port"), "J1")
        assert {"R1", "R2", "R3", "C1", "C2"} <= set(data["served_by"])

    def test_resolves_by_hierarchical_path_too(self) -> None:
        data = describe_component(load("ldo-supply"), "rail_3v3.U")
        assert data["refdes"] == "U1"

    def test_unknown_component_raises(self) -> None:
        with pytest.raises(KeyError):
            describe_component(load("usb-port"), "Q99")


class TestNets:
    def test_describes_a_net_with_its_rules(self) -> None:
        data = describe_net(load("usb-port"), "USB_DP")
        assert data["class"] == "usb"
        assert data["impedance_ohm"] == 90.0
        assert data["diff_pair"] == "USB_DM"
        assert data["rules"]["diff_pair_gap_mm"] == 0.2

    def test_net_class_collects_every_member(self) -> None:
        data = nets_of_class(load("usb-port"), "usb")
        assert data["count"] == 4
        assert {n["net"] for n in data["nets"]} == {
            "USB_DP", "USB_DM", "DEV_DP", "DEV_DM"
        }

    def test_unknown_class_is_empty_not_an_error(self) -> None:
        assert nets_of_class(load("usb-port"), "nope")["count"] == 0


class TestRoles:
    def test_finds_components_by_role(self) -> None:
        data = components_by_role(load("usb-port"), "series")
        assert {c["refdes"] for c in data["components"]} == {"R1", "R2"}

    def test_carries_reasons(self) -> None:
        data = components_by_role(load("led-blinker"), "decoupling")
        assert data["count"] >= 1
        assert all(c.get("reason") for c in data["components"])


@needs_kicad_libraries
class TestQueryCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", *args],
            capture_output=True, text=True, check=False,
        )

    def test_summary_renders(self, example_design: Path) -> None:
        result = self._run("summary", str(example_design))
        assert result.returncode == 0, result.stderr
        assert "blocks:" in result.stdout

    def test_summary_json(self, example_design: Path) -> None:
        payload = json.loads(self._run("summary", str(example_design), "--json").stdout)
        assert payload["blocks"]

    def test_query_module(self) -> None:
        design = str(REPO_ROOT / "examples" / "ldo-supply" / "design.yaml")
        result = self._run("query", "module", design, "rail_3v3")
        assert result.returncode == 0, result.stderr
        assert "neighbours:" in result.stdout

    def test_query_net_class(self) -> None:
        design = str(REPO_ROOT / "examples" / "usb-port" / "design.yaml")
        result = self._run("query", "net-class", design, "usb")
        assert "diff_pair_gap_mm" in result.stdout

    def test_unknown_name_suggests_alternatives(self) -> None:
        design = str(REPO_ROOT / "examples" / "ldo-supply" / "design.yaml")
        result = self._run("query", "module", design, "rail3v3")
        assert result.returncode == 1
        assert "did you mean" in result.stderr

    def test_unknown_component_lists_what_exists(self) -> None:
        design = str(REPO_ROOT / "examples" / "usb-port" / "design.yaml")
        result = self._run("query", "component", design, "ZZ9")
        assert result.returncode == 1
        assert "available:" in result.stderr

    def test_text_and_json_agree(self) -> None:
        """One structure, two renderings -- they cannot describe different designs."""
        design = str(REPO_ROOT / "examples" / "usb-port" / "design.yaml")
        payload = json.loads(self._run("query", "component", design, "R1", "--json").stdout)
        text = self._run("query", "component", design, "R1").stdout
        assert payload["part"] in text
        for connection in payload["connections"]:
            assert connection["net"] in text

"""Signal-integrity simulation: slices, solver inputs, and the arithmetic on the way back.

Nothing here starts a container. The solver is pinned, slow and external; what these
tests are for is everything on *this* side of it -- that the right pairs are found,
that a slice is a reproducible board with its ports where they belong, that the files
gerber2ems reads say what the source says, and that the S-parameter arithmetic gives
the textbook answer on a case whose answer is known.

The one test that costs real time (`TestSliceExport`) runs kicad-cli, because the two
defects M12 found in this path were both invisible in the data structures and visible
only in the exported files.
"""

from __future__ import annotations

import cmath
import csv
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from aipcb.compile.build import compile_netlist
from aipcb.compile.export import export_board
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import dump, parse
from aipcb.model.layout import Stackup, StackupLayer
from aipcb.model.simulation import SimulationSettings
from aipcb.si.inputs import (
    CELLS_ACROSS_GAP,
    CELLS_ACROSS_TRACE,
    dielectrics,
    grid_optimal_um,
    max_steps,
    netinfo_json,
    simulation_json,
    stackup_json,
)
from aipcb.si.pairs import logical_pairs
from aipcb.si.results import SParameters, analyse, read_sparameters, write_touchstone
from aipcb.si.runner import nets_in_gerbers, slice_digest
from aipcb.si.slice import SliceError, build_slice

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries


@pytest.fixture(scope="module")
def routed_pcie(tmp_path_factory: pytest.TempPathFactory):
    """`examples/pcie-sata`, built and routed once for the whole module."""
    return _routed("pcie-sata", tmp_path_factory.mktemp("pcie"))


@pytest.fixture(scope="module")
def routed_mcu(tmp_path_factory: pytest.TempPathFactory):
    return _routed("mcu-4layer", tmp_path_factory.mktemp("mcu"))


def _routed(name: str, where: Path):
    from aipcb.route.pipeline import route_design

    design = REPO_ROOT / "examples" / name / "design.yaml"
    report = Report()
    done = route_design(design, where, report)
    return done.build.netlist, done.board


# ---------------------------------------------------------------------------
# which pairs there are
# ---------------------------------------------------------------------------


class TestLogicalPairs:
    def test_pcie_sata_has_eleven_links_not_twelve_declared_pairs(self) -> None:
        """The transmit lane is two declared pairs and one physical link.

        `PCIE_TXP/N` runs to the coupling capacitors and `PCIE_TXP_C/N_C` runs on
        from them. Twelve declared pairs, eleven links -- and simulating the twelve
        would put a port in the middle of a capacitor.
        """
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", Report()
        )
        pairs = logical_pairs(netlist)
        assert len(pairs) == 11
        merged = next(p for p in pairs if p.name == "PCIE_TXN+PCIE_TXP")
        assert merged.positive == ("PCIE_TXN", "PCIE_TXN_C")
        assert merged.negative == ("PCIE_TXP", "PCIE_TXP_C")
        assert merged.bridged_by == ("C2", "C3")
        assert len(merged.declared) == 2

    def test_a_pair_that_is_not_coupled_stays_one_pair(self) -> None:
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", Report()
        )
        sata = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        assert sata.positive == ("SATA0_TXN",)
        assert sata.bridged_by == ()

    def test_the_order_is_a_function_of_the_names(self) -> None:
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", Report()
        )
        names = [p.name for p in logical_pairs(netlist)]
        assert names == sorted(names)

    def test_a_design_with_no_pairs_returns_nothing(self) -> None:
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "led-blinker" / "design.yaml", Report()
        )
        assert logical_pairs(netlist) == []


# ---------------------------------------------------------------------------
# the slice
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestSlice:
    def _slice(self, routed, name: str, ohm: float = 42.5):
        netlist, board = routed
        pair = next(p for p in logical_pairs(netlist) if p.name == name)
        settings = netlist.simulation.for_class(pair.net_class)
        return build_slice(board, netlist, pair, settings, port_impedance_ohm=ohm)

    def test_every_pcie_sata_pair_can_be_sliced(self, routed_pcie) -> None:
        netlist, board = routed_pcie
        for pair in logical_pairs(netlist):
            settings = netlist.simulation.for_class(pair.net_class)
            sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
            assert len(sliced.ports) == 4, pair.name

    def test_a_slice_is_byte_stable(self, routed_pcie) -> None:
        """Same board in, same file out. A slice is an artifact, not a snapshot."""
        first = dump(self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP").board)
        second = dump(self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP").board)
        assert first == second

    def test_ports_sit_at_the_ends_of_the_pair_and_face_inwards(
        self, routed_pcie
    ) -> None:
        sliced = self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP")
        assert [p.number for p in sliced.ports] == [1, 2, 3, 4]
        assert {p.rotation for p in sliced.ports} <= {0.0, 90.0, 180.0, 270.0}
        assert {p.net for p in sliced.ports} == {"SATA0_TXN", "SATA0_TXP"}
        for port in sliced.ports:
            assert sliced.rect[0] < port.at[0] < sliced.rect[2]
            assert sliced.rect[1] < port.at[1] < sliced.rect[3]

    def test_ports_are_referenced_to_the_plane_the_stackup_declares(
        self, routed_pcie
    ) -> None:
        """`pcie_tx` runs on F.Cu, whose declared reference is the GND plane on In1."""
        sliced = self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP")
        assert {p.layer_index for p in sliced.ports} == {0}
        assert {p.plane_index for p in sliced.ports} == {1}

    def test_a_pair_that_changes_layer_says_so(self, routed_pcie) -> None:
        """`REFCLKP/N` starts on one layer and ends on another; a reader must be told."""
        sliced = self._slice(routed_pcie, "REFCLKN+REFCLKP")
        assert len({p.layer for p in sliced.ports}) == 2
        assert any("changes layer" in note for note in sliced.notes)

    def test_which_pairs_span_layers_is_a_property_and_not_prose(
        self, routed_pcie
    ) -> None:
        """M13.5. The two links that miss +/-10 % are exactly the two that span
        layers, so whether a slice does needs to be readable rather than inferred
        from a sentence in `notes`. `spans_planes` goes with it because a layer
        change on this board is also a change of reference plane, and it is the
        plane change that makes the reported impedance not a trace impedance.
        """
        spanning = {
            name: (s.spans_layers, s.spans_planes)
            for name in ("REFCLKN+REFCLKP", "PCIE_RXN+PCIE_RXP",
                         "PCIE_TXN+PCIE_TXP", "SATA0_TXN+SATA0_TXP")
            if (s := self._slice(routed_pcie, name))
        }
        assert spanning["REFCLKN+REFCLKP"] == (True, True)
        assert spanning["PCIE_RXN+PCIE_RXP"] == (True, True)
        assert spanning["PCIE_TXN+PCIE_TXP"] == (False, False)
        assert spanning["SATA0_TXN+SATA0_TXP"] == (False, False)
        assert self._slice(routed_pcie, "REFCLKN+REFCLKP").to_dict()["spans_layers"]

    def test_no_copper_reaches_the_slice_outline(self, routed_pcie) -> None:
        """gerber2ems frames every layer on Edge.Cuts; copper past it moves the frame."""
        sliced = self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP")
        minx, miny, maxx, maxy = sliced.rect
        for segment in sliced.board.children("segment"):
            width = float(segment.child("width").value() or 0)
            for point in ("start", "end"):
                node = segment.child(point)
                x, y = float(node.value(0) or 0), float(node.value(1) or 0)
                assert minx < x - width / 2 and x + width / 2 < maxx
                assert miny < y - width / 2 and y + width / 2 < maxy

    def test_the_coupling_capacitors_become_copper(self, routed_pcie) -> None:
        """The bridge closes the transmit lane, so it has two ends and not four."""
        sliced = self._slice(routed_pcie, "PCIE_TXN+PCIE_TXP")
        assert len(sliced.ports) == 4
        assert sliced.bridged == ("C2", "C3")

    def test_every_net_in_the_slice_is_declared_in_it(self, routed_pcie) -> None:
        sliced = self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP")
        declared = {
            int(n.value(0) or 0) for n in sliced.board.children("net")
        }
        used = {
            int(s.child("net").value() or 0) for s in sliced.board.children("segment")
        }
        assert used <= declared

    def test_each_port_carries_a_pad_so_kicad_keeps_its_net(self, routed_pcie) -> None:
        """The defect this guards is silent: KiCad prunes padless nets on load.

        It prunes by renumbering, so the tracks come back wearing a neighbour's name,
        the Gerber's net attributes are wrong, the mesh generator refines nothing and
        the solver returns a short circuit at exit code zero.
        """
        sliced = self._slice(routed_pcie, "SATA0_TXN+SATA0_TXP")
        pads = [
            pad
            for fp in sliced.board.children("footprint")
            for pad in fp.children("pad")
        ]
        assert len(pads) == 4
        for pad in pads:
            size = pad.child("size")
            assert float(size.value(0) or 0) <= 0.06, "the keeper pad adds copper"
            assert pad.child("net") is not None

    def test_the_two_launches_of_one_end_never_share_a_line(self, routed_pcie) -> None:
        """The defect this guards produced a 437 ohm reading on an 85 ohm pair.

        A pair that leaves its pads broadside has both endpoints separated along one
        axis. Launch along that same axis and the two ports run on one line, overlap,
        and short P to N -- with a plausible-looking board and a clean exit code. So
        the launch axis has to be the one the endpoints do *not* separate along, and
        the test asks that of every pair on the reference board.
        """
        netlist, board = routed_pcie
        for pair in logical_pairs(netlist):
            settings = netlist.simulation.for_class(pair.net_class)
            sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
            ports = {port.number: port for port in sliced.ports}
            for near, far in ((1, 3), (2, 4)):
                a, b = ports[near], ports[far]
                separation = (b.at[0] - a.at[0], b.at[1] - a.at[1])
                along_x = abs(separation[0]) >= abs(separation[1])
                launched_along_x = a.rotation in (90.0, 270.0)
                assert along_x != launched_along_x, (
                    f"{pair.name}: ports {near} and {far} are launched along the axis "
                    "they are separated on, so they overlap"
                )

    def test_the_launch_corridor_has_no_foreign_copper_left_in_it(
        self, routed_pcie
    ) -> None:
        """A launch runs out past the pad, where the next pins' fanout lives.

        On `examples/pcie-sata` every pair end has a ground track crossing within a
        millimetre. Left in place, the launch is welded to ground and the solver
        reports the short at exit code zero, so the slice cuts the corridor out and
        says how much it removed.
        """
        from shapely.geometry import LineString

        netlist, board = routed_pcie
        pair = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        settings = netlist.simulation.for_class(pair.net_class)
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
        assert any("removed from the corridor" in n for n in sliced.notes)

        # The port's body runs from its outer face back towards the trace; the
        # rotation says which way. Written out because it is the same mapping the
        # solver reads out of the placement file.
        bodies = {0.0: (0, -1), 90.0: (-1, 0), 180.0: (0, 1), 270.0: (1, 0)}
        corridors = [
            LineString(
                [
                    port.at,
                    (
                        port.at[0] + bodies[port.rotation][0] * port.length_mm,
                        port.at[1] + bodies[port.rotation][1] * port.length_mm,
                    ),
                ]
            ).buffer(port.width_mm / 2 + 0.10)
            for port in sliced.ports
        ]

        codes = {int(n.value(0) or 0): n.value(1) for n in sliced.board.children("net")}
        for segment in sliced.board.children("segment"):
            if codes.get(int(segment.child("net").value() or 0)) in sliced.pair.nets:
                continue
            line = LineString(
                [
                    (float(segment.child("start").value(0) or 0),
                     float(segment.child("start").value(1) or 0)),
                    (float(segment.child("end").value(0) or 0),
                     float(segment.child("end").value(1) or 0)),
                ]
            )
            for number, shape in enumerate(corridors, start=1):
                assert not line.intersects(shape), (
                    f"foreign copper still runs through port {number}'s launch"
                )

    def test_an_unrouted_pair_is_refused_by_name(self, routed_mcu) -> None:
        netlist, _ = routed_mcu
        pair = next(p for p in logical_pairs(netlist))
        settings = netlist.simulation.for_class(pair.net_class)
        empty = parse(
            '(kicad_pcb (version 20241229) (generator "aipcb") (net 0 "") (setup))'
        )
        with pytest.raises(SliceError) as exc:
            build_slice(empty, netlist, pair, settings, port_impedance_ohm=50.0)
        assert exc.value.code == "si-pair-unrouted"


# ---------------------------------------------------------------------------
# what gerber2ems is told
# ---------------------------------------------------------------------------


class TestSolverInputs:
    def test_the_stackup_follows_the_source_not_the_uniform_default(self) -> None:
        """`pcie-sata` declares a 0.2104 mm prepreg under F.Cu, not a third of 1.44.

        This is the disagreement M12 found: `compile/board.py` writes KiCad three
        equal 0.48 mm dielectrics, while impedance is derived from the declared
        stack. Simulating the KiCad copy would measure a board nobody described.
        """
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "pcie-sata" / "design.yaml", Report()
        )
        assert netlist.layout is not None
        laminate = dielectrics(netlist.layout.stackup)
        assert [round(d.thickness_mm, 4) for d in laminate] == [0.2104, 1.065, 0.2104]
        assert [d.epsilon_r for d in laminate] == [4.4, 4.6, 4.4]

    def test_the_stackup_json_interleaves_metal_and_laminate_front_to_back(self) -> None:
        stackup = Stackup(copper_layers=4, thickness_mm=1.6)
        kinds = [layer["type"] for layer in stackup_json(stackup)["layers"]]
        assert kinds == [
            "Top Silk Screen", "Top Solder Paste", "Top Solder Mask",
            "copper", "core", "copper", "core", "copper", "core", "copper",
            "Bottom Solder Mask", "Bottom Solder Paste", "Bottom Silk Screen",
        ]

    def test_loss_tangent_is_reachable_from_source(self) -> None:
        """Until M12 it was hardcoded at 0.02 and no design could say otherwise.

        Above about a gigahertz dielectric loss is what insertion loss *is*, so a
        number nobody could set made the honesty clause about matching the fab's
        stackup vacuous.
        """
        stackup = Stackup(
            copper_layers=2,
            thickness_mm=1.6,
            layers=(
                StackupLayer(name="F.Cu", type="copper", thickness_mm=0.035),
                StackupLayer(
                    name="core", type="core", thickness_mm=1.51,
                    material="Megtron6", epsilon_r=3.4, loss_tangent=0.002,
                ),
                StackupLayer(name="B.Cu", type="copper", thickness_mm=0.035),
            ),
        )
        laminate = dielectrics(stackup)
        assert laminate[0].loss_tangent == 0.002
        assert laminate[0].material == "Megtron6"

    def test_a_missing_loss_tangent_falls_back_to_what_kicad_is_told(self) -> None:
        assert dielectrics(Stackup(copper_layers=2))[0].loss_tangent == 0.02

    @needs_kicad_libraries
    def test_every_length_in_simulation_json_is_micrometres(self, routed_pcie) -> None:
        """The unit is nowhere in the upstream README and is a factor of a thousand."""
        netlist, board = routed_pcie
        pair = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        settings = netlist.simulation.for_class(pair.net_class)
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
        payload = simulation_json(sliced, settings)
        port = payload["ports"][0]
        assert port["width"] == pytest.approx(sliced.ports[0].width_mm * 1000)
        assert port["length"] == pytest.approx(settings.launch_mm * 1000)
        # The cell is micrometres too, and since M13b it is *derived* rather than
        # copied: never coarser than the setting, fine enough for the geometry.
        assert 0 < payload["grid"]["optimal"] <= settings.grid_optimal_um
        assert payload["grid"]["optimal"] == pytest.approx(
            grid_optimal_um(sliced, settings)
        )

    @needs_kicad_libraries
    def test_the_mesh_is_derived_from_the_trace_and_the_gap(self, routed_pcie) -> None:
        """M13b. A fixed cell returned 12.2 ohm for an 85 ohm line; see ADR 0012.

        The rule is six cells across the trace and five across the gap, and what it
        has to guarantee is that a *narrower* pair gets a *finer* mesh without
        anybody remembering to ask for one.
        """
        netlist, board = routed_pcie
        settings = netlist.simulation.for_class("sata")
        pair = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)

        cell = grid_optimal_um(sliced, settings)
        assert cell < settings.grid_optimal_um, "this pair is fine enough to need it"
        assert sliced.ports[0].width_mm * 1000 / cell >= CELLS_ACROSS_TRACE
        assert sliced.pair_gap_mm is not None
        assert sliced.pair_gap_mm * 1000 / cell >= CELLS_ACROSS_GAP

        # And the step limit follows it, because an FDTD timestep is set by the
        # cell: a fixed limit would make the finer run stop earlier, not cost less.
        assert max_steps(sliced, settings) > settings.max_steps

    @needs_kicad_libraries
    def test_the_rule_is_never_coarser_than_the_setting(self, routed_mcu) -> None:
        """A design that asks for a fine mesh gets one; the rule only refines.

        `examples/mcu-4layer` is the board M12 calibrated on, and its 0.25 mm pair
        at a 0.2 mm gap comes out at **41.6 um** rather than the declared 50 --
        wide geometry, but not wide enough to be left alone. That is inside the
        25-100 um band M12's own convergence sweep covered on this exact pair, over
        which the answer moved 5.3 %, so the calibration still bounds what runs; it
        is not the same as the default being untouched, and the number is asserted
        here rather than assumed anywhere.
        """
        netlist, board = routed_mcu
        settings = netlist.simulation.for_class("usb")
        pair = next(p for p in logical_pairs(netlist) if "USB" in p.name)
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=45.0)

        assert grid_optimal_um(sliced, settings) == pytest.approx(41.6, abs=0.5)
        assert 25.0 <= grid_optimal_um(sliced, settings) <= 50.0

        # Ask for finer and the rule stands aside: it refines, it never coarsens.
        fine = settings.model_copy(update={"grid_optimal_um": 10.0})
        assert grid_optimal_um(sliced, fine) == 10.0
        assert max_steps(sliced, fine) == fine.max_steps

    @needs_kicad_libraries
    def test_the_driven_end_is_both_halves_of_the_pair(self, routed_pcie) -> None:
        netlist, board = routed_pcie
        pair = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        settings = netlist.simulation.for_class(pair.net_class)
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
        payload = simulation_json(sliced, settings)
        assert [p["excite"] for p in payload["ports"]] == [True, False, True, False]
        pair_cfg = payload["differential_pairs"][0]
        assert (pair_cfg["start_p"], pair_cfg["stop_p"]) == (0, 1)
        assert (pair_cfg["start_n"], pair_cfg["stop_n"]) == (2, 3)
        assert set(pair_cfg["nets"]) == set(sliced.pair.nets)

    @needs_kicad_libraries
    def test_netinfo_names_the_pair_and_nothing_else(self, routed_pcie) -> None:
        netlist, board = routed_pcie
        pair = next(p for p in logical_pairs(netlist) if p.name.startswith("SATA0_TX"))
        settings = netlist.simulation.for_class(pair.net_class)
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=50.0)
        assert netinfo_json(sliced) == {
            "nets": [{"name": "SATA0_TXN"}, {"name": "SATA0_TXP"}]
        }


class TestSettings:
    def test_a_class_override_wins_and_the_rest_falls_back(self) -> None:
        settings = SimulationSettings.model_validate(
            {"stop_ghz": 8.0, "classes": {"sata": {"stop_ghz": 6.0}}}
        )
        assert settings.for_class("sata").stop_hz == 6e9
        assert settings.for_class("pcie_rx").stop_hz == 8e9
        assert settings.for_class("sata").margin_mm == settings.margin_mm

    def test_a_design_that_says_nothing_still_has_settings(self) -> None:
        netlist = compile_netlist(
            REPO_ROOT / "examples" / "diff-pair" / "design.yaml", Report()
        )
        assert netlist.simulation.for_class("lvds").stop_hz > 0


# ---------------------------------------------------------------------------
# the export, which is where the silent failures live
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def exported_slice(tmp_path_factory: pytest.TempPathFactory):
    """One `mcu-4layer` slice, exported by kicad-cli, for the file-level checks."""
    from aipcb.route.pipeline import route_design

    where = tmp_path_factory.mktemp("slice-export")
    report = Report()
    done = route_design(
        REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml", where / "routed", report
    )
    netlist = done.build.netlist
    pair = next(p for p in logical_pairs(netlist) if p.name == "USB_DM+USB_DP")
    settings = netlist.simulation.for_class(pair.net_class)
    sliced = build_slice(done.board, netlist, pair, settings, port_impedance_ohm=45.0)
    work = where / "slice"
    work.mkdir()
    (work / "slice.kicad_pcb").write_text(dump(sliced.board), encoding="utf-8")
    result = export_board(work / "slice.kicad_pcb", work / "fab", netlist, report)
    assert result.ok, report.render()
    return sliced, work


@needs_kicad_libraries
@needs_kicad_cli
class TestSliceExport:
    """The three checks that only reading the exported files can make."""

    def test_the_pairs_nets_survive_into_the_gerbers(self, exported_slice) -> None:
        """The X2 net attribute is what the mesh generator keys on.

        A slice has few footprints and KiCad drops every net without a pad -- by
        renumbering, so the copper comes back labelled with a neighbour's net. The
        geometry is untouched, which is exactly why nothing catches it except reading
        the attribute back.
        """
        sliced, work = exported_slice
        found = nets_in_gerbers(work / "fab")
        assert set(sliced.pair.nets) <= found, f"the Gerbers carry {sorted(found)}"

    def test_the_placement_file_puts_the_ports_where_the_slice_says(
        self, exported_slice
    ) -> None:
        sliced, work = exported_slice
        path = next((work / "fab").glob("*pos.csv"))
        rows = [
            row
            for row in csv.DictReader(path.read_text(encoding="utf-8").splitlines())
            if "Simulation_Port" in row["Package"]
        ]
        assert len(rows) == 4, "gerber2ems globs for these and exits if it finds none"
        by_ref = {row["Ref"]: row for row in rows}
        for port in sliced.ports:
            row = by_ref[f"SP{port.number}"]
            assert float(row["PosX"]) == pytest.approx(
                port.at[0] - sliced.origin[0], abs=1e-3
            )
            assert float(row["PosY"]) == pytest.approx(
                sliced.origin[1] - port.at[1], abs=1e-3
            )
            assert float(row["PosX"]) >= 0 and float(row["PosY"]) >= 0
            assert float(row["Rot"]) % 360 == port.rotation % 360

    def test_the_plated_drill_file_is_where_gerber2ems_looks(
        self, exported_slice
    ) -> None:
        _, work = exported_slice
        assert len(list((work / "fab").glob("*-PTH.drl"))) == 1


# ---------------------------------------------------------------------------
# the arithmetic on the way back
# ---------------------------------------------------------------------------


def _ideal_pair(z0: float, zdiff: float, points: int = 201) -> SParameters:
    """A lossless, perfectly terminated differential line, in S-parameters.

    The odd-mode reflection is zero when the line's differential impedance equals
    twice the port reference; the even mode is whatever it is and must not affect
    the answer. Written out rather than simulated so the expected result is known
    exactly: gerber2ems's extraction has to give `zdiff` back.
    """
    frequencies = [1e9 + i * 1e7 for i in range(points)]
    values: dict[tuple[int, int], list[complex]] = {}
    gamma_odd = (zdiff / 2 - z0) / (zdiff / 2 + z0)
    gamma_even = 0.3
    for into in (0, 2):
        other = 2 if into == 0 else 0
        far = into + 1
        far_other = other + 1
        values[(into, into)] = [
            complex((gamma_even + gamma_odd) / 2, 0) for _ in frequencies
        ]
        values[(other, into)] = [
            complex((gamma_even - gamma_odd) / 2, 0) for _ in frequencies
        ]
        values[(far, into)] = [complex(0.5, 0) for _ in frequencies]
        values[(far_other, into)] = [complex(-0.5, 0) for _ in frequencies]
    return SParameters(
        frequencies=frequencies, excited=(0, 2), ports=4, values=values
    )


class TestResults:
    def _settings(self, stop_hz: float = 8e9):
        return SimulationSettings().for_class("x").model_copy(
            update={"stop_hz": stop_hz}
        )

    def test_a_matched_line_comes_back_at_its_own_impedance(self) -> None:
        metrics = analyse(
            _ideal_pair(z0=50.0, zdiff=100.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=self._settings(),
        )
        assert metrics is not None
        assert metrics.impedance_ohm == pytest.approx(100.0, rel=1e-6)
        assert metrics.verdicts["impedance"] == "pass"

    def test_a_layer_spanning_link_says_its_number_is_not_a_trace_impedance(
        self,
    ) -> None:
        """M13.5. The estimator is a median input impedance, which is the
        characteristic impedance of a *uniform* line. A link that changes layer is
        a cascade of two sections and a via barrel referenced to two planes, so the
        number is the whole link's. It is still published -- it is a real
        measurement -- and it stops being offered as a trace impedance.
        """
        common = dict(
            pair="X", net_class="c", port_impedance_ohm=50.0, target_ohm=100.0,
            settings=self._settings(),
        )
        uniform = analyse(_ideal_pair(z0=50.0, zdiff=100.0), **common)
        spanning = analyse(
            _ideal_pair(z0=50.0, zdiff=100.0), spans_layers=True, **common
        )
        assert uniform is not None and spanning is not None
        assert uniform.spans_layers is False
        assert spanning.spans_layers is True
        assert not any("different layers" in n for n in uniform.notes)
        assert any("different layers" in n for n in spanning.notes)
        # the number itself is untouched: this is a caveat, not a correction
        assert spanning.impedance_ohm == uniform.impedance_ohm
        assert spanning.to_dict()["spans_layers"] is True

    def test_a_line_off_target_is_measured_not_rounded_to_the_port(self) -> None:
        """Ports are set to half the target, so a 130 ohm line must read 130."""
        metrics = analyse(
            _ideal_pair(z0=50.0, zdiff=130.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=self._settings(),
        )
        assert metrics is not None
        assert metrics.impedance_ohm == pytest.approx(130.0, rel=1e-6)
        assert metrics.deviation == pytest.approx(0.30, rel=1e-3)
        assert metrics.verdicts["impedance"] == "warn"

    def test_gain_is_reported_as_unusable_rather_than_as_a_number(self) -> None:
        """|Sdd21| above one is energy from nowhere: the marker phase 0 found."""
        sp = _ideal_pair(z0=50.0, zdiff=100.0)
        sp.values[(1, 0)] = [complex(1.4, 0) for _ in sp.frequencies]
        metrics = analyse(
            sp, pair="X", net_class="c", port_impedance_ohm=50.0,
            target_ohm=100.0, settings=self._settings(),
        )
        assert metrics is not None
        assert metrics.verdicts["impedance"] == "unusable"
        assert any("not physical" in note for note in metrics.notes)

    def test_a_matrix_with_only_one_excitation_is_not_analysed(self) -> None:
        sp = _ideal_pair(z0=50.0, zdiff=100.0)
        for key in [k for k in sp.values if k[1] == 2]:
            del sp.values[key]
        assert analyse(
            sp, pair="X", net_class="c", port_impedance_ohm=50.0,
            target_ohm=100.0, settings=self._settings(),
        ) is None

    def test_touchstone_is_readable_and_says_what_it_does_not_know(
        self, tmp_path: Path
    ) -> None:
        sp = _ideal_pair(z0=50.0, zdiff=100.0)
        path = tmp_path / "x.s4p"
        write_touchstone(sp, path, 50.0)
        lines = path.read_text(encoding="utf-8").splitlines()
        option = next(line for line in lines if line.startswith("#"))
        assert option.split() == ["#", "HZ", "S", "RI", "R", "50"]
        assert any("were not run" in line for line in lines)
        data = [line for line in lines if not line.startswith(("#", "!"))]
        assert len(data) == len(sp.frequencies)
        assert len(data[0].split()) == 1 + 2 * 16

    def test_the_csv_reader_survives_the_trailing_comma(self, tmp_path: Path) -> None:
        """gerber2ems writes a trailing delimiter on every row, header included."""
        sim = tmp_path / "simulation"
        sim.mkdir()
        header = "Frequency [MHz], " + "".join(
            f"re(S{i}-0), " for i in range(4)
        ) + "".join(f"im(S{i}-0), " for i in range(4))
        rows = [header, "100, " + ", ".join(["0.1"] * 8) + ", "]
        (sim / "Sx0.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        sp = read_sparameters(sim)
        assert sp.frequencies == [1e8]
        assert sp.s(0, 0) == [complex(0.1, 0.1)]

    def test_group_delay_matches_a_line_of_known_length(self) -> None:
        """Six picoseconds a millimetre on FR-4; a delay far from that is a wrong port."""
        frequencies = [1e9 + i * 1e7 for i in range(201)]
        delay_s = 200e-12
        values: dict[tuple[int, int], list[complex]] = {}
        for into in (0, 2):
            other = 2 if into == 0 else 0
            for out, amplitude in (
                (into, 0.0), (other, 0.0), (into + 1, 0.5), (other + 1, -0.5)
            ):
                values[(out, into)] = [
                    amplitude
                    * complex(
                        math.cos(-2 * math.pi * f * delay_s),
                        math.sin(-2 * math.pi * f * delay_s),
                    )
                    for f in frequencies
                ]
        sp = SParameters(frequencies, (0, 2), 4, values)
        metrics = analyse(
            sp, pair="X", net_class="c", port_impedance_ohm=50.0,
            target_ohm=None, settings=SimulationSettings().for_class("x"),
        )
        assert metrics is not None
        assert metrics.delay_ns == pytest.approx(0.2, rel=1e-3)


# ---------------------------------------------------------------------------
# the command, and the cache
# ---------------------------------------------------------------------------


@needs_kicad_libraries
@needs_kicad_cli
class TestSimulateCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "simulate", *args],
            capture_output=True, text=True, check=False,
        )

    def test_dry_run_writes_every_solver_input_and_starts_nothing(
        self, tmp_path: Path
    ) -> None:
        design = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"
        run = self._run(str(design), "--out", str(tmp_path / "si"), "--dry-run")
        assert run.returncode == 0, run.stdout + run.stderr
        work = tmp_path / "si" / "USB_DM+USB_DP"
        for name in ("slice.kicad_pcb", "simulation.json", "netinfo.json", "slice.json"):
            assert (work / name).exists(), name
        assert (work / "fab" / "stackup.json").exists()
        assert not (work / "ems").exists(), "a dry run started the solver"

    def test_the_digest_moves_only_when_the_slice_does(self, tmp_path: Path) -> None:
        """What makes a re-run free: the cache key is what the solver reads."""
        design = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"
        first = tmp_path / "a"
        assert self._run(str(design), "--out", str(first), "--dry-run").returncode == 0
        before = slice_digest(first / "USB_DM+USB_DP")

        second = tmp_path / "b"
        assert self._run(str(design), "--out", str(second), "--dry-run").returncode == 0
        assert slice_digest(second / "USB_DM+USB_DP") == before

        widened = tmp_path / "widened.yaml"
        text = (design.read_text(encoding="utf-8")
                .replace("- ../library/", f"- {REPO_ROOT / 'examples' / 'library'}/"))
        widened.write_text(
            text + "\nsimulation:\n  stop_ghz: 3.0\n  reason: cache test\n",
            encoding="utf-8",
        )
        third = tmp_path / "c"
        assert self._run(str(widened), "--out", str(third), "--dry-run").returncode == 0
        assert slice_digest(third / "USB_DM+USB_DP") != before

    def test_json_names_every_pair_including_the_ones_not_simulated(
        self, tmp_path: Path
    ) -> None:
        design = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"
        run = self._run(
            str(design), "--out", str(tmp_path / "si"), "--dry-run",
            "--net", "USB_DP", "--json",
        )
        assert run.returncode == 0, run.stdout + run.stderr
        payload = json.loads(run.stdout)["simulation"]
        assert [p["pair"] for p in payload["pairs"]] == ["USB_DM+USB_DP"]
        assert [p["pair"] for p in payload["not_simulated"]] == ["DEV_DM+DEV_DP"]

    def test_an_unknown_net_is_an_error_not_an_empty_run(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "mcu-4layer" / "design.yaml"
        run = self._run(
            str(design), "--out", str(tmp_path / "si"), "--dry-run", "--net", "NOPE"
        )
        assert run.returncode == 2
        assert "no differential pair" in run.stderr


# ---------------------------------------------------------------------------
# M13c: the skew verdict, read across frequency
# ---------------------------------------------------------------------------


def _pair_with_skew(
    delay_ps: float,
    floor_db: float = -300.0,
    points: int = 401,
    transit_ps: float = 120.0,
):
    """A pair whose only defect is a known intra-pair delay, plus a flat floor.

    Written out rather than solved, so the answer the fit has to recover is known
    exactly. Skew converts differential to common as ``|sin(pi f dt)|``; the floor
    stands for everything else a real board converts -- launch asymmetry, via
    barrels, the pour around each trace -- and is flat, which is what makes the two
    separable at all.

    The columns are chosen backwards from the mixed-mode combinations the analyser
    takes, so that ``Sdd21`` comes out as the transmission and ``Scd21`` as the
    conversion:

        Sdd21 = (S10 - S12 - S30 + S32) / 2
        Scd21 = (S10 - S12 + S30 - S32) / 2

    ``transit_ps`` is the one-way propagation, carried as a phase ramp so the group
    delay the analyser measures is a real number rather than zero.
    """
    frequencies = [5e8 + i * 2e7 for i in range(points)]
    floor = 10 ** (floor_db / 20)
    values: dict[tuple[int, int], list[complex]] = {}
    for reflected in ((0, 0), (2, 0), (0, 2), (2, 2)):
        values[reflected] = [0j for _ in frequencies]
    through: list[complex] = []
    converted: list[complex] = []
    for hz in frequencies:
        conversion = math.hypot(math.sin(math.pi * hz * delay_ps * 1e-12), floor)
        magnitude = math.sqrt(max(0.0, 1.0 - conversion**2))
        phase = cmath.exp(-2j * math.pi * hz * transit_ps * 1e-12)
        through.append(magnitude * phase)
        converted.append(conversion * phase)
    values[(1, 0)] = [(t + c) / 2 for t, c in zip(through, converted, strict=True)]
    values[(1, 2)] = [-(t + c) / 2 for t, c in zip(through, converted, strict=True)]
    values[(3, 0)] = [-(t - c) / 2 for t, c in zip(through, converted, strict=True)]
    values[(3, 2)] = [(t - c) / 2 for t, c in zip(through, converted, strict=True)]
    return SParameters(frequencies=frequencies, excited=(0, 2), ports=4, values=values)


class TestSkewFit:
    """M13c. The verdict M12 could not take from a scalar.

    M12's answer to "does simulation discriminate the two pairs M11 delivered over
    budget" was no, and worse than no: the three links its `mode_conversion` scalar
    flagged were the three *best*-matched pairs on the board. The cause was not that
    the physics is invisible -- `REFCLKP/N` tracks `|sin(pi f dt)|` to within 3.2 dB
    across its band -- it was that a worst-in-band maximum reads the floor, and the
    floor varied by more than 25 dB between pairs while the skew moved it by 3.
    """

    def test_a_known_delay_is_recovered_from_the_curve(self) -> None:
        from aipcb.si.results import _mixed_mode, fit_skew

        for delay_ps in (1.0, 2.0, 5.0, 10.0):
            sp = _pair_with_skew(delay_ps)
            mixed = _mixed_mode(sp)
            assert mixed is not None
            _, _, sdd21, scd21 = mixed
            fit = fit_skew(sp.frequencies, sdd21, scd21, list(range(len(sp.frequencies))))
            assert fit is not None
            assert fit.delay_ps == pytest.approx(delay_ps, abs=0.05), delay_ps
            assert fit.confident

    def test_a_pair_with_no_skew_fits_no_skew(self) -> None:
        from aipcb.si.results import _mixed_mode, fit_skew

        sp = _pair_with_skew(0.0, floor_db=-30.0)
        mixed = _mixed_mode(sp)
        assert mixed is not None
        _, _, sdd21, scd21 = mixed
        fit = fit_skew(sp.frequencies, sdd21, scd21, list(range(len(sp.frequencies))))
        assert fit is not None
        assert not fit.confident, fit.to_dict()

    def test_a_skew_buried_under_the_floor_is_an_upper_bound(self) -> None:
        """The honest third answer, and the one a scalar cannot express."""
        from aipcb.si.results import _mixed_mode, fit_skew

        sp = _pair_with_skew(0.05, floor_db=-25.0)
        mixed = _mixed_mode(sp)
        assert mixed is not None
        _, _, sdd21, scd21 = mixed
        fit = fit_skew(sp.frequencies, sdd21, scd21, list(range(len(sp.frequencies))))
        assert fit is not None
        assert not fit.confident, fit.to_dict()

    def test_the_floor_is_recovered_too(self) -> None:
        from aipcb.si.results import _mixed_mode, fit_skew

        sp = _pair_with_skew(5.0, floor_db=-30.0)
        mixed = _mixed_mode(sp)
        assert mixed is not None
        _, _, sdd21, scd21 = mixed
        fit = fit_skew(sp.frequencies, sdd21, scd21, list(range(len(sp.frequencies))))
        assert fit is not None
        assert fit.floor_db == pytest.approx(-30.0, abs=1.0), fit.to_dict()

    def test_the_fit_is_deterministic(self) -> None:
        from aipcb.si.results import _mixed_mode, fit_skew

        sp = _pair_with_skew(3.0, floor_db=-35.0)
        mixed = _mixed_mode(sp)
        assert mixed is not None
        _, _, sdd21, scd21 = mixed
        band = list(range(len(sp.frequencies)))
        first = fit_skew(sp.frequencies, sdd21, scd21, band)
        second = fit_skew(sp.frequencies, sdd21, scd21, band)
        assert first == second

    def test_the_verdict_is_taken_against_the_classs_own_budget(self) -> None:
        settings = SimulationSettings().for_class("x").model_copy(
            update={"stop_hz": 8e9}
        )
        # 5 ps over 40 mm of conductor -- 20 mm each way -- at ~6 ps/mm is a hair
        # under a millimetre of length mismatch.
        metrics = analyse(
            _pair_with_skew(5.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=settings,
            length_mm=40.0,
            geometric_skew_mm=0.9,
            max_skew_mm=0.25,
        )
        assert metrics is not None
        assert metrics.skew_fit is not None
        assert metrics.verdicts["skew_fit"] == "warn"
        assert metrics.fitted_skew_mm is not None
        assert metrics.fitted_skew_mm > 0.25

    def test_a_pair_inside_its_budget_passes(self) -> None:
        settings = SimulationSettings().for_class("x").model_copy(
            update={"stop_hz": 8e9}
        )
        metrics = analyse(
            _pair_with_skew(5.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=settings,
            length_mm=40.0,
            max_skew_mm=5.0,
        )
        assert metrics is not None
        assert metrics.verdicts["skew_fit"] == "pass"

    def test_the_scalar_verdict_is_labelled_low_confidence(self) -> None:
        """It stays, and it stops pretending to be a skew verdict."""
        settings = SimulationSettings().for_class("x").model_copy(
            update={"stop_hz": 8e9, "mode_conversion_db": -40.0}
        )
        metrics = analyse(
            _pair_with_skew(5.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=settings,
            length_mm=40.0,
        )
        assert metrics is not None
        assert metrics.verdicts["mode_conversion"] == "warn-low-confidence"

    def test_delay_becomes_a_length_with_the_pairs_own_propagation(self) -> None:
        """Both halves are counted in the conductor length; the signal travels one."""
        settings = SimulationSettings().for_class("x").model_copy(
            update={"stop_hz": 8e9}
        )
        metrics = analyse(
            _pair_with_skew(5.0),
            pair="X",
            net_class="c",
            port_impedance_ohm=50.0,
            target_ohm=100.0,
            settings=settings,
            length_mm=40.0,
            max_skew_mm=0.25,
        )
        assert metrics is not None
        assert metrics.ps_per_mm is not None
        assert metrics.delay_ns is not None
        assert metrics.ps_per_mm == pytest.approx(
            metrics.delay_ns * 1000 / 20.0, rel=1e-9
        )


# ---------------------------------------------------------------------------
# M13d: the container outlives nothing
# ---------------------------------------------------------------------------


def _runtime_and_image() -> tuple[str, str] | None:
    """A container runtime with the pinned image, or ``None`` to skip."""
    from aipcb.si import IMAGE
    from aipcb.si.runner import ContainerMissing, container_digest, find_container

    try:
        runtime = find_container()
        container_digest(runtime, IMAGE)
    except (ContainerMissing, OSError):
        return None
    return runtime, IMAGE


needs_container = pytest.mark.skipif(
    _runtime_and_image() is None,
    reason="no container runtime with the pinned gerber2ems image",
)


class TestContainerLifetime:
    """The solver is a child of the runtime, not of this process (M13d).

    Nothing about `aipcb` dying stops it. M12 cleaned up on the timeout path only,
    and the M10-M12 chain paid for that twice: a killed session left a sixteen-core
    FDTD run going eleven minutes later, and a relaunch then had two containers
    writing one working directory.
    """

    def test_the_block_reaps_on_the_way_out(self, monkeypatch) -> None:
        from aipcb.si import runner

        calls: list[list[str]] = []
        monkeypatch.setattr(
            runner.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd))
        )
        with runner.running_container("podman", "aipcb-si-test"):
            assert "aipcb-si-test" in runner._LIVE
        assert calls == [["podman", "rm", "-f", "aipcb-si-test"]]
        assert "aipcb-si-test" not in runner._LIVE

    def test_it_reaps_on_an_exception_too(self, monkeypatch) -> None:
        from aipcb.si import runner

        calls: list[list[str]] = []
        monkeypatch.setattr(
            runner.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd))
        )
        with pytest.raises(KeyboardInterrupt), runner.running_container(
            "podman", "aipcb-si-boom"
        ):
            raise KeyboardInterrupt
        assert calls == [["podman", "rm", "-f", "aipcb-si-boom"]]

    def test_reaping_twice_is_harmless(self, monkeypatch) -> None:
        from aipcb.si import runner

        calls: list[list[str]] = []
        monkeypatch.setattr(
            runner.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd))
        )
        with runner.running_container("podman", "aipcb-si-once"):
            pass
        assert runner.reap_containers() == []
        assert len(calls) == 1

    def test_a_busy_directory_is_refused_rather_than_shared(
        self, monkeypatch, tmp_path
    ) -> None:
        """The one path that survives SIGKILL, which nothing can catch."""
        from aipcb.si import runner

        monkeypatch.setattr(
            runner, "containers_on", lambda runtime, work: ["aipcb-si-someone-else"]
        )
        with pytest.raises(runner.ContainerBusy) as caught:
            runner.run_gerber2ems(tmp_path, runtime="podman")
        assert "aipcb-si-someone-else" in str(caught.value)
        assert str(tmp_path) in str(caught.value)

    def test_the_preflight_asks_the_runtime_about_this_directory(
        self, monkeypatch, tmp_path
    ) -> None:
        from aipcb.si import runner

        seen: dict[str, list[str]] = {}

        class Result:
            returncode = 0
            stdout = "aipcb-si-1\naipcb-si-2\n"

        def fake_run(cmd, **kw):
            seen["cmd"] = list(cmd)
            return Result()

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        assert runner.containers_on("podman", tmp_path) == ["aipcb-si-1", "aipcb-si-2"]
        assert f"label={runner.WORK_LABEL}={tmp_path.resolve()}" in seen["cmd"]

    @needs_container
    def test_a_killed_client_leaves_no_orphan(self, tmp_path) -> None:
        """The milestone's own test: kill a run mid-solve, assert nothing survives.

        Driven through a real container rather than a fake one, because what is
        under test is whether a *process* dying takes a *container* with it, and a
        monkeypatched ``subprocess.run`` cannot answer that.
        """
        import os
        import signal
        import time

        found = _runtime_and_image()
        assert found is not None
        runtime, image = found
        work = tmp_path / "solving"
        work.mkdir()

        script = f"""
import os, subprocess, sys, time
sys.path.insert(0, {str(REPO_ROOT / "src")!r})
from aipcb.si.runner import running_container, containers_on, WORK_LABEL
work = {str(work.resolve())!r}
name = f"aipcb-si-{{os.getpid()}}-killtest"
with running_container({runtime!r}, name):
    subprocess.Popen(
        [{runtime!r}, "run", "--rm", "--name", name,
         "--label", f"{{WORK_LABEL}}={{work}}", "--entrypoint", "sleep",
         {image!r}, "600"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if containers_on({runtime!r}, __import__("pathlib").Path(work)):
            break
        time.sleep(0.2)
    print("up", flush=True)
    time.sleep(120)
"""
        path = tmp_path / "solve.py"
        path.write_text(script, encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, str(path)], stdout=subprocess.PIPE, text=True
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "up"
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=60)
        finally:
            if child.poll() is None:  # pragma: no cover - only on a hang
                child.kill()
                child.wait(timeout=30)

        from aipcb.si.runner import containers_on

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not containers_on(runtime, work):
                break
            time.sleep(0.5)
        orphans = containers_on(runtime, work)
        for name in orphans:  # pragma: no cover - only when the test fails
            subprocess.run([runtime, "rm", "-f", name], capture_output=True)
        assert not orphans, orphans
        assert os.getpid() != child.pid

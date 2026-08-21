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
from aipcb.si.inputs import dielectrics, netinfo_json, simulation_json, stackup_json
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
        assert payload["grid"]["optimal"] == settings.grid_optimal_um

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

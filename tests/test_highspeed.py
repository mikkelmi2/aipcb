"""Controlled impedance, card edges, via transitions and the M11e report (M11).

Four kinds of test, and the split is the same one M10 used:

* **arithmetic** -- the impedance formulas and the stackup resolution need nothing
  but numbers, and are asserted against hand-computed values;
* **validation** -- what the source may say and what `aipcb validate` says back,
  including the two error messages that are supposed to be *useful*: the missing
  keying notch, which hands the vertices back, and the width override that
  disagrees with its own impedance target;
* **generation** -- the transition pattern, asserted off the geometry it produces
  rather than off the parameters that asked for it;
* **the board** -- the reference example, checked once and asserted against many
  times, because filling and routing it costs three quarters of a minute.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from aipcb.checks.accouple import ac_couplings, run_ac_coupling_checks
from aipcb.checks.edge import EDGE_TOLERANCE_MM, run_edge_checks
from aipcb.checks.highspeed import STUB_WARN_MM, analyse_highspeed
from aipcb.checks.impedance import run_impedance_checks
from aipcb.checks.loop import check_design
from aipcb.compile.build import build_design
from aipcb.compile.edge import edge_connectors, footprint_edge_paths
from aipcb.compile.frame import frame_for
from aipcb.compile.place import component_extents, plan_placement
from aipcb.compile.zones import finger_keepout_uuid
from aipcb.diagnostics import Report, Severity
from aipcb.elaborate import elaborate
from aipcb.highspeed import controlled_classes, target_for
from aipcb.impedance import (
    DEFAULT_EPSILON_R,
    FAR_GAP_MM,
    ImpedanceUnreachable,
    coplanar_odd_factor,
    cpwg_differential,
    cpwg_microstrip,
    differential_impedance,
    elliptic_ratio,
    grounded_cpw,
    hammerstad_microstrip,
    ipc2141_microstrip,
    solve_width,
)
from aipcb.kicad.footprints import resolve_footprint
from aipcb.kicad.sexpr import parse
from aipcb.loader import load_design
from aipcb.model.layout import Stackup, StackupLayer
from aipcb.route.obstacles import extract_obstacles
from aipcb.route.plan import route_board
from aipcb.route.stack import stack_for
from aipcb.route.transition import (
    MAX_TRANSITION_VIAS,
    generate_transitions,
    transition_uuid,
    transition_uuids,
)

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

PCIE_SATA = REPO_ROOT / "examples" / "pcie-sata" / "design.yaml"
LIBRARY = REPO_ROOT / "examples" / "library"

#: The reference stackup's numbers, from ADR 0010. Every derived geometry in this
#: file is checked against a value computed by hand from these.
PREPREG_MM = 0.2104
PREPREG_ER = 4.4
COPPER_MM = 0.035


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------


class TestImpedance:
    def test_the_reference_stackup_gives_the_documented_widths(self) -> None:
        """ADR 0010's two numbers, recomputed rather than remembered."""
        pcie = solve_width(85.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        sata = solve_width(100.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert pcie.width_mm == pytest.approx(0.322, abs=0.001)
        assert sata.width_mm == pytest.approx(0.239, abs=0.001)

    def test_the_solved_width_reproduces_its_own_target(self) -> None:
        for target in (75.0, 85.0, 90.0, 100.0, 110.0):
            geometry = solve_width(target, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER)
            assert differential_impedance(
                geometry.width_mm, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER
            ) == pytest.approx(target, abs=0.05)

    def test_bisection_is_deterministic(self) -> None:
        first = solve_width(85.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        second = solve_width(85.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert first == second

    def test_a_target_outside_the_bracket_is_refused_not_clamped(self) -> None:
        with pytest.raises(ImpedanceUnreachable):
            solve_width(400.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        with pytest.raises(ImpedanceUnreachable):
            solve_width(0.5, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)

    def test_the_two_formulas_disagree_by_about_eight_percent(self) -> None:
        """The finding that decided which one the controlled-impedance path uses.

        Copper thickness is the whole of the difference: IPC-2141 has it in the
        denominator and Hammerstad, as this project implements it, does not.
        """
        ipc = ipc2141_microstrip(0.322, PREPREG_MM, COPPER_MM, PREPREG_ER)
        hammerstad = hammerstad_microstrip(0.322, PREPREG_MM, PREPREG_ER)
        assert 0.06 < (hammerstad - ipc) / ipc < 0.12

    def test_a_wider_trace_is_a_lower_impedance(self) -> None:
        wide = differential_impedance(0.5, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        narrow = differential_impedance(0.2, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert wide < narrow

    def test_a_wider_gap_is_a_higher_impedance(self) -> None:
        near = differential_impedance(0.3, 0.1, PREPREG_MM, COPPER_MM, PREPREG_ER)
        far = differential_impedance(0.3, 0.6, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert far > near


class TestCoplanarGround:
    """M13b. The model M12 measured the absence of, and the one it did not choose."""

    def test_ground_alongside_lowers_the_impedance(self) -> None:
        bare = differential_impedance(0.25, 0.2, 0.48, COPPER_MM, DEFAULT_EPSILON_R)
        poured = cpwg_differential(
            0.25, 0.2, 0.48, COPPER_MM, DEFAULT_EPSILON_R, pour_gap=0.2
        )
        assert poured < bare
        # And the closer it is, the further down it pulls.
        tighter = cpwg_differential(
            0.25, 0.2, 0.48, COPPER_MM, DEFAULT_EPSILON_R, pour_gap=0.1
        )
        assert tighter < poured

    def test_a_pour_far_away_is_the_bare_microstrip_exactly(self) -> None:
        """The property that keeps every pre-M13 board's geometry where it was."""
        for width, gap, height in ((0.25, 0.2, 0.48), (0.322, 0.2, 0.2104)):
            bare = differential_impedance(
                width, gap, height, COPPER_MM, DEFAULT_EPSILON_R
            )
            assert cpwg_differential(
                width, gap, height, COPPER_MM, DEFAULT_EPSILON_R, FAR_GAP_MM
            ) == pytest.approx(bare, rel=1e-9)

    def test_the_factor_is_between_a_half_and_one(self) -> None:
        for gap in (0.05, 0.1, 0.15, 0.2, 0.5, 2.0):
            factor = coplanar_odd_factor(0.15, gap, 0.2104)
            assert 0.3 < factor <= 1.0
            assert coplanar_odd_factor(0.15, gap, 0.2104) >= coplanar_odd_factor(
                0.15, gap / 2, 0.2104
            )

    def test_a_single_ended_trace_gets_two_neighbours_not_one(self) -> None:
        """Ground on both sides loads the trace twice as much as one neighbour."""
        bare = ipc2141_microstrip(0.25, 0.48, COPPER_MM, DEFAULT_EPSILON_R)
        one_side = bare * (1 - 0.48 * math.exp(-0.96 * 0.2 / 0.48))
        both = cpwg_microstrip(0.25, 0.2, 0.48, COPPER_MM, DEFAULT_EPSILON_R)
        assert both < one_side < bare

    def test_the_solver_hits_its_target_under_the_coplanar_model(self) -> None:
        for target in (85.0, 100.0):
            geometry = solve_width(
                target, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER, pour_gap=0.15
            )
            assert geometry.model == "cpwg"
            assert geometry.pour_gap_mm == 0.15
            assert cpwg_differential(
                geometry.width_mm, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER, 0.15
            ) == pytest.approx(target, abs=0.05)

    def test_the_coplanar_model_derives_a_narrower_trace(self) -> None:
        """M12 said the derived widths were 'systematically narrow'. They were wide.

        Impedance falls as a trace widens, so a board reading *below* its target was
        built too wide, and correcting the model has to narrow it. This is the
        direction, asserted, because the chain report has it the other way round.
        """
        bare = solve_width(85.0, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER)
        poured = solve_width(
            85.0, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER, pour_gap=0.15
        )
        assert poured.width_mm < bare.width_mm
        assert poured.width_mm == pytest.approx(0.1846, abs=0.0005)
        assert bare.width_mm == pytest.approx(0.2888, abs=0.0005)

    def test_the_published_conformal_form_is_kept_and_says_what_it_is(self) -> None:
        """The measurement that chose against the Wadell/Simons closed form.

        Both numbers are in `impedance.py`'s docstrings; asserting them here is what
        stops the ADR's reasoning becoming a claim nobody can check.
        """
        # Inside its domain -- a real 50 ohm conductor-backed CPW on RO4350.
        assert grounded_cpw(1.0, 0.25, 0.508, 3.48) == pytest.approx(50.0, abs=2.0)
        # Outside it: an isolated trace should read what a microstrip reads, and
        # this reads twenty percent high, because its er_eff tends to er.
        isolated = grounded_cpw(0.2888, FAR_GAP_MM, PREPREG_MM, PREPREG_ER)
        microstrip = ipc2141_microstrip(0.2888, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert isolated > microstrip * 1.2

    def test_the_elliptic_ratio_is_exact_at_the_self_complementary_point(self) -> None:
        """``K(k)/K(k') = 1`` at ``k = 1/sqrt(2)``, where ``k = k'``."""
        assert elliptic_ratio(1 / math.sqrt(2)) == pytest.approx(1.0, abs=1e-12)
        assert elliptic_ratio(0.3) < 1.0 < elliptic_ratio(0.9)


def _four_layer_stack() -> Stackup:
    return Stackup(
        copper_layers=4,
        thickness_mm=1.6,
        layers=(
            StackupLayer(name="F.Cu", type="copper", thickness_mm=0.035),
            StackupLayer(
                name="pre", type="prepreg", thickness_mm=PREPREG_MM, epsilon_r=4.4
            ),
            StackupLayer(name="In1.Cu", type="copper", thickness_mm=0.0152),
            StackupLayer(
                name="core", type="core", thickness_mm=1.065, epsilon_r=4.6
            ),
            StackupLayer(name="In2.Cu", type="copper", thickness_mm=0.0152),
            StackupLayer(
                name="pre2", type="prepreg", thickness_mm=PREPREG_MM, epsilon_r=4.4
            ),
            StackupLayer(name="B.Cu", type="copper", thickness_mm=0.035),
        ),
        planes=({"layer": "In1.Cu", "net": "GND"},),  # type: ignore[arg-type]
    )


class TestStackup:
    def test_a_declared_stack_is_honoured(self) -> None:
        stack = _four_layer_stack()
        assert stack.declared_stack is not None
        assert stack.dielectric_between("F.Cu", "In1.Cu").thickness_mm == PREPREG_MM
        assert stack.dielectric_between("F.Cu", "In1.Cu").epsilon_r == PREPREG_ER
        assert stack.copper_thickness_mm("In1.Cu") == 0.0152

    def test_the_thicknesses_add_across_several_dielectrics(self) -> None:
        stack = _four_layer_stack()
        whole = stack.dielectric_between("F.Cu", "B.Cu")
        assert whole.thickness_mm == pytest.approx(2 * PREPREG_MM + 1.065, abs=1e-6)
        assert 4.4 < whole.epsilon_r < 4.6

    def test_a_partial_declaration_is_ignored_rather_than_half_believed(self) -> None:
        stack = Stackup(
            copper_layers=4,
            layers=(StackupLayer(name="F.Cu", type="copper", thickness_mm=0.035),),
        )
        assert stack.declared_stack is None
        assert stack.dielectric_between(
            "F.Cu", "In1.Cu"
        ).thickness_mm == pytest.approx(stack.dielectric_thickness_mm)

    def test_an_undeclared_stack_falls_back_to_the_uniform_arithmetic(self) -> None:
        stack = Stackup(copper_layers=2)
        assert stack.declared_stack is None
        assert stack.epsilon_r_default == DEFAULT_EPSILON_R
        assert stack.dielectric_between("F.Cu", "B.Cu").thickness_mm == pytest.approx(
            stack.dielectric_thickness_mm
        )

    def test_the_reference_is_the_nearest_declared_plane(self) -> None:
        stack = _four_layer_stack()
        assert stack.reference_below("F.Cu") == "In1.Cu"
        assert stack.reference_below("B.Cu") == "In1.Cu"
        assert Stackup().reference_below("F.Cu") is None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


CLASS_BASE = """
name: impedance-test
libraries: ["{library}/passives.yaml"]
net_classes:
  hs:
    trace_width_mm: 0.2
    clearance_mm: 0.15
    diff_pair_gap_mm: 0.2
{extra}
nets:
  A_P: {{class: hs, diff_pair: A_N}}
  A_N: {{class: hs, diff_pair: A_P}}
components:
  R1:
    part: R_10K_0603
    pins: {{"1": A_P, "2": A_N}}
  R2:
    part: R_10K_0603
    pins: {{"1": A_P, "2": A_N}}
board:
  outline:
    rect: [30.0, 20.0]
layout:
  stackup:
    copper_layers: 4
    thickness_mm: 1.6
    epsilon_r: 4.4
    layers:
      - {{name: F.Cu, type: copper, thickness_mm: 0.035}}
      - {{name: pre, type: prepreg, thickness_mm: 0.2104, epsilon_r: 4.4}}
      - {{name: In1.Cu, type: copper, thickness_mm: 0.0152}}
      - {{name: core, type: core, thickness_mm: 1.065, epsilon_r: 4.6}}
      - {{name: In2.Cu, type: copper, thickness_mm: 0.0152}}
      - {{name: pre2, type: prepreg, thickness_mm: 0.2104, epsilon_r: 4.4}}
      - {{name: B.Cu, type: copper, thickness_mm: 0.035}}
    planes:
      - {{layer: In1.Cu, net: GND}}
pours:
  - net: GND
    layer: In1.Cu
    scope: board
"""


def impedance_codes(extra: str, write_design) -> dict[str, str]:
    """Run the M11a source checks over a class with ``extra`` fields."""
    report = Report()
    design = load_design(
        write_design(CLASS_BASE.format(extra=extra, library=LIBRARY)), report=report
    )
    netlist = elaborate(design, report)
    fresh = Report()
    run_impedance_checks(netlist, fresh)
    return {d.code: d.message for d in fresh.diagnostics}


def _source_with(extra: str, pours: str, clearance: float | None) -> str:
    source = CLASS_BASE.format(extra=extra, library=LIBRARY) + pours
    if clearance is not None:
        source = source.replace("    clearance_mm: 0.15\n", f"    clearance_mm: {clearance}\n")
    return source


def impedance_target(extra: str, write_design, pours: str = "", clearance=None):
    """Resolve one class's target from a source, pours and all (M13b)."""
    source = _source_with(extra, pours, clearance)
    report = Report()
    design = load_design(write_design(source), report=report)
    netlist = elaborate(design, report)
    return netlist, target_for(netlist, "hs")


F_CU_POUR = """  - net: GND
    layer: F.Cu
    scope: board
"""

#: The same pour, hugging the pair. Both clearances have to be tight for the gap to
#: be: KiCad enforces the larger of the two, so a tight net class beside a pour that
#: keeps its distance is still a comfortable gap.
F_CU_POUR_TIGHT = """  - net: GND
    layer: F.Cu
    scope: board
    clearance: 0.05
"""


@needs_kicad_libraries
class TestImpedanceModelSelection:
    """Which model a class gets, and why. M13b.

    The choice is made from the `pours:` block rather than from a flag, because
    what decides it is a fact about the board: is there ground beside this pair or
    is there not. M11 solved every class as a bare microstrip on boards that all
    poured ground up to their pairs, and M12 measured the 40 % that cost.
    """

    HS = (
        "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
        "    prefer_layers: [F.Cu]\n"
    )

    def test_no_pour_on_the_pairs_layer_is_a_microstrip(self, write_design) -> None:
        _, target = impedance_target(self.HS, write_design)
        assert target is not None
        assert target.model == "microstrip"
        assert target.pour_gap_mm is None
        assert target.gap_sensitivity is None

    def test_a_pour_on_the_pairs_layer_is_a_coplanar_waveguide(
        self, write_design
    ) -> None:
        _, target = impedance_target(self.HS, write_design, pours=F_CU_POUR)
        assert target is not None
        assert target.model == "cpwg"
        assert target.pour_gap_mm is not None

    def test_the_gap_is_the_larger_of_the_two_clearances(self, write_design) -> None:
        """KiCad enforces the larger, so the derivation has to predict the larger."""
        _, wide = impedance_target(
            self.HS, write_design, pours=F_CU_POUR, clearance=0.35
        )
        _, narrow = impedance_target(
            self.HS, write_design, pours=F_CU_POUR, clearance=0.1
        )
        assert wide is not None and narrow is not None
        assert wide.pour_gap_mm == pytest.approx(0.35)
        # 0.1 is narrower than the ground class's own default clearance, so the
        # pour's figure wins rather than the signal's.
        assert narrow.pour_gap_mm is not None
        assert narrow.pour_gap_mm > 0.1

    def test_the_coplanar_class_derives_a_narrower_trace(self, write_design) -> None:
        _, bare = impedance_target(self.HS, write_design)
        _, poured = impedance_target(self.HS, write_design, pours=F_CU_POUR)
        assert bare is not None and poured is not None
        assert poured.geometry.width_mm < bare.geometry.width_mm

    def test_the_model_reaches_the_targets_own_dictionary(self, write_design) -> None:
        _, target = impedance_target(self.HS, write_design, pours=F_CU_POUR)
        assert target is not None
        published = target.to_dict()
        assert published["model"] == "cpwg"
        assert published["pour_gap_mm"] == target.pour_gap_mm
        assert published["gap_sensitivity"] is not None


@needs_kicad_libraries
class TestPourGapSensitivity:
    """The coupling M13b introduced, surfaced before a board is made.

    The pour clearance used to be a DRC number and nothing else. Now it is an input
    to the width derivation, so a class whose pour sits tight enough that one etch
    tolerance moves the impedance materially has spent part of its budget on a
    fabrication tolerance rather than on the trace.
    """

    HS = (
        "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
        "    prefer_layers: [F.Cu]\n"
    )

    def _codes(
        self, extra: str, write_design, pours: str = "", clearance: float | None = None
    ) -> dict[str, str]:
        source = _source_with(extra, pours, clearance)
        report = Report()
        design = load_design(write_design(source), report=report)
        netlist = elaborate(design, report)
        fresh = Report()
        run_impedance_checks(netlist, fresh)
        return {d.code: d.message for d in fresh.diagnostics}

    def test_a_comfortable_pour_gap_says_nothing(self, write_design) -> None:
        codes = self._codes(self.HS, write_design, pours=F_CU_POUR, clearance=0.15)
        assert "impedance-pour-gap-sensitive" not in codes

    def test_a_tight_pour_gap_is_reported(self, write_design) -> None:
        codes = self._codes(
            self.HS, write_design, pours=F_CU_POUR_TIGHT, clearance=0.05
        )
        assert "impedance-pour-gap-sensitive" in codes, codes
        message = codes["impedance-pour-gap-sensitive"]
        assert "0.05" in message and "85 ohm" in message

    def test_the_threshold_is_the_classs_own_to_set(self, write_design) -> None:
        assert "impedance-pour-gap-sensitive" in self._codes(
            self.HS, write_design, pours=F_CU_POUR_TIGHT, clearance=0.05
        )
        assert "impedance-pour-gap-sensitive" not in self._codes(
            self.HS + "    pour_gap_sensitivity: 0.2\n",
            write_design, pours=F_CU_POUR_TIGHT, clearance=0.05,
        )

    def test_a_class_with_no_pour_beside_it_is_not_asked(self, write_design) -> None:
        codes = self._codes(self.HS, write_design, clearance=0.05)
        assert "impedance-pour-gap-sensitive" not in codes

    def test_the_gap_is_the_pours_own_when_it_is_the_larger(self, write_design) -> None:
        """A tight net class beside a pour that keeps its distance is not tight."""
        codes = self._codes(self.HS, write_design, pours=F_CU_POUR, clearance=0.05)
        assert "impedance-pour-gap-sensitive" not in codes


@needs_kicad_libraries
class TestImpedanceValidation:
    def test_a_class_without_a_target_says_nothing(self, write_design) -> None:
        assert impedance_codes("", write_design) == {}

    def test_a_reachable_target_says_nothing(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
            "    prefer_layers: [F.Cu]\n",
            write_design,
        )
        assert codes == {}

    def test_an_explicit_width_that_disagrees_is_reported(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
            "    prefer_layers: [F.Cu]\n    diff_pair_width_mm: 0.45\n",
            write_design,
        )
        assert "impedance-geometry-override" in codes
        message = codes["impedance-geometry-override"]
        assert "0.322" in message, message
        assert "+40%" in message, message

    def test_a_width_within_ten_percent_is_not_reported(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
            "    prefer_layers: [F.Cu]\n    diff_pair_width_mm: 0.34\n",
            write_design,
        )
        assert "impedance-geometry-override" not in codes

    def test_a_reference_the_board_does_not_have_is_an_error(
        self, write_design
    ) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In7.Cu\n", write_design
        )
        assert "impedance-reference-missing" in codes

    def test_a_reference_that_is_not_a_plane_is_a_warning(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In2.Cu\n"
            "    prefer_layers: [F.Cu]\n",
            write_design,
        )
        assert "impedance-reference-not-a-plane" in codes

    def test_a_reference_that_is_its_own_layer_is_an_error(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: F.Cu\n"
            "    prefer_layers: [F.Cu]\n",
            write_design,
        )
        assert "impedance-reference-is-signal-layer" in codes

    def test_tight_coupling_without_a_budget_is_reported(self, write_design) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 85\n    reference: In1.Cu\n"
            "    prefer_layers: [F.Cu]\n    coupling: tight\n",
            write_design,
        )
        assert "impedance-no-uncoupled-budget" in codes

    def test_highspeed_fields_without_a_target_are_reported_as_inert(
        self, write_design
    ) -> None:
        codes = impedance_codes("    standoff_k: 3.0\n    coupling: tight\n", write_design)
        assert "impedance-rules-inert" in codes

    def test_an_unreachable_target_is_reported_with_the_geometry(
        self, write_design
    ) -> None:
        codes = impedance_codes(
            "    impedance_diff_ohm: 240\n    reference: In1.Cu\n"
            "    prefer_layers: [F.Cu]\n",
            write_design,
        )
        assert "impedance-unreachable" in codes
        assert "240 ohm" in codes["impedance-unreachable"]
        assert "range this stackup can reach" in codes["impedance-unreachable"]


@needs_kicad_libraries
class TestDerivedGeometry:
    def test_the_example_derives_the_widths_the_adrs_record(self) -> None:
        """ADR 0010's numbers, and what ADR 0012 changed them to.

        M11 derived these against a bare microstrip and M12 measured every one of
        them below its target, because the board pours ground 0.15 mm from each
        pair. Both sets are asserted here rather than the old ones simply being
        replaced: the *change* is the finding, and a test that only knew the new
        numbers would let the model quietly drift back.
        """
        report = Report()
        netlist = elaborate(load_design(PCIE_SATA, report=report), report)
        targets = controlled_classes(netlist)
        assert set(targets) == {"pcie_rx", "pcie_tx", "sata"}

        # Every class on this board is solved coplanar, because every one of them
        # has ground poured beside it at the class clearance.
        for name in ("pcie_rx", "pcie_tx", "sata"):
            assert targets[name].model == "cpwg", name
            assert targets[name].pour_gap_mm == pytest.approx(0.15)

        assert targets["sata"].geometry.width_mm == pytest.approx(0.1379, abs=0.001)
        assert targets["sata"].gap_mm == 0.2
        assert targets["pcie_tx"].geometry.width_mm == pytest.approx(0.1846, abs=0.001)
        assert targets["pcie_tx"].reference == "In1.Cu"
        assert targets["pcie_rx"].reference == "In2.Cu"
        assert targets["pcie_rx"].layer == "B.Cu"

        # ADR 0010's own numbers, still reachable: the same targets on the same
        # stackup with nothing poured alongside.
        bare_sata = solve_width(100.0, 0.2, PREPREG_MM, COPPER_MM, PREPREG_ER)
        bare_pcie = solve_width(85.0, 0.15, PREPREG_MM, COPPER_MM, PREPREG_ER)
        assert bare_sata.width_mm == pytest.approx(0.239, abs=0.001)
        assert bare_pcie.width_mm == pytest.approx(0.2888, abs=0.001)
        assert bare_sata.model == "microstrip"

    def test_the_derivation_uses_the_declared_prepreg_not_a_uniform_stack(
        self,
    ) -> None:
        report = Report()
        netlist = elaborate(load_design(PCIE_SATA, report=report), report)
        target = target_for(netlist, "sata")
        assert target is not None
        assert target.geometry.height_mm == PREPREG_MM
        assert target.geometry.epsilon_r == PREPREG_ER
        # The uniform arithmetic would have given a third of the board thickness.
        assert netlist.layout is not None
        assert netlist.layout.stackup.dielectric_thickness_mm > 0.4


# ---------------------------------------------------------------------------
# the card edge (M11b)
# ---------------------------------------------------------------------------


def edge_codes(text: str, path: Path) -> dict[str, Report]:
    report = Report()
    netlist = elaborate(load_design(path, report=report), report)
    fresh = Report()
    run_edge_checks(netlist, fresh)
    del text
    return {"report": fresh}  # type: ignore[dict-item]


@needs_kicad_libraries
class TestEdgeConnector:
    def _checked(self, source: str, tmp_path: Path) -> Report:
        design = tmp_path / "design.yaml"
        design.write_text(source, encoding="utf-8")
        report = Report()
        netlist = elaborate(load_design(design, report=report), report)
        fresh = Report()
        run_edge_checks(netlist, fresh)
        return fresh

    def _source(self) -> str:
        text = PCIE_SATA.read_text(encoding="utf-8")
        return text.replace("../library/", str(LIBRARY) + "/")

    def test_the_example_outline_matches_the_footprint(self, tmp_path: Path) -> None:
        report = self._checked(self._source(), tmp_path)
        codes = {d.code for d in report.diagnostics}
        assert "edge-connector-outline-matches" in codes
        assert not report.errors, [d.render() for d in report.errors]

    def test_a_missing_notch_is_an_error_that_hands_the_vertices_back(
        self, tmp_path: Path
    ) -> None:
        """The message has to be a fix, not a complaint.

        Deleting the keying notch from the outline leaves the rest of the card
        edge in place, so this is the *partial* case: most of the footprint's
        geometry still matches, and only the notch does not.
        """
        source = self._source().replace(
            """      - [35.05, 0.5]
      - [35.05, 7.45]
      - { arc_to: [36.95, 7.45], center: [36.0, 7.45], direction: cw }
      - [36.95, 0.5]
      - [37.45, 0.0]
""",
            "",
        )
        report = self._checked(source, tmp_path)
        errors = [d for d in report.errors if d.code == "edge-connector-notch-missing"]
        assert errors, [d.code for d in report.diagnostics]
        hint = errors[0].hint or ""
        assert "board:" in hint and "polygon:" in hint
        # The notch's own corners, in the source's frame, ready to paste.
        assert "35.05" in hint and "36.95" in hint, hint

    def test_a_connector_off_the_edge_is_a_different_error(
        self, tmp_path: Path
    ) -> None:
        source = self._source().replace(
            "fixed: { x: 24.5, y: 3.45, rot: 0 }",
            "fixed: { x: 24.5, y: 25.0, rot: 0 }",
        )
        report = self._checked(source, tmp_path)
        codes = {d.code for d in report.errors}
        assert "edge-connector-off-edge" in codes

    def test_an_unfixed_edge_connector_is_an_error(self, tmp_path: Path) -> None:
        source = self._source().replace(
            "fixed: { x: 24.5, y: 3.45, rot: 0 }",
            "region: { rect: [[10.0, 1.0], [50.0, 8.0]] }",
        )
        report = self._checked(source, tmp_path)
        assert "edge-connector-not-fixed" in {d.code for d in report.errors}

    def test_a_board_the_slot_will_not_take_is_a_warning(self, tmp_path: Path) -> None:
        source = self._source().replace("thickness_mm: 1.6", "thickness_mm: 2.4")
        report = self._checked(source, tmp_path)
        warnings = {d.code for d in report.warnings}
        assert "edge-connector-thickness" in warnings

    def test_the_footprint_draws_the_notch_this_test_relies_on(self) -> None:
        """Guard: if KiCad's footprint changes, the tests above stop meaning much."""
        footprint = resolve_footprint("Connector_PCBEdge:BUS_PCIexpress_x1")
        paths = footprint_edge_paths(footprint.node)
        assert len(paths) == 11
        xs = [x for path in paths for x, _ in path]
        assert min(xs) == pytest.approx(-0.65)
        assert max(xs) == pytest.approx(19.65)


@pytest.fixture(scope="module")
def board(tmp_path_factory: pytest.TempPathFactory):
    """The reference board, built once: everything below reads it, none writes it."""
    target = tmp_path_factory.mktemp("edge")
    result = build_design(PCIE_SATA, out_dir=target)
    path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    return parse(path.read_text(encoding="utf-8")), result.netlist


@needs_kicad_libraries
class TestEdgeIntegration:
    """What the *board* gets, rather than what validation says about the source."""

    def test_the_footprints_own_edge_graphics_are_not_emitted(self, board) -> None:
        """ADR 0010: the board outline has one author, and it is the `board:` block."""
        tree, _ = board
        connector = next(
            fp
            for fp in tree.children("footprint")
            if any(
                p.value(0) == "Reference" and p.value(1) == "J1"
                for p in fp.children("property")
            )
        )
        drawn = [
            item
            for item in connector.children()
            if item.name.startswith("fp_") and item.get("layer") == "Edge.Cuts"
        ]
        assert not drawn, f"{len(drawn)} Edge.Cuts primitives survived into the board"

    def test_the_finger_field_gets_a_pour_keepout(self, board) -> None:
        tree, netlist = board
        zone = next(
            (z for z in tree.children("zone") if z.get("uuid") == finger_keepout_uuid("J1")),
            None,
        )
        assert zone is not None, "no keepout zone was emitted for the card edge"
        keepout = zone.child("keepout")
        assert keepout is not None
        assert keepout.get("copperpour") == "not_allowed"
        assert keepout.get("tracks") == "allowed", (
            "the pairs have to reach the fingers; only the pour is kept out"
        )
        layers = [str(a.value) for a in (zone.child("layers") or zone).atoms()]
        assert layers == ["F.Cu", "B.Cu"], (
            "only the outer layers are plated, and the inner plane under the "
            "fingers is the reference the pairs entering them are designed against"
        )
        del netlist

    def test_the_keepout_covers_every_finger(self, board) -> None:
        tree, netlist = board
        frame = frame_for(netlist)
        extents, missing = component_extents(netlist)
        assert not missing
        placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
        connector = edge_connectors(netlist, placement)[0]
        x1, y1, x2, y2 = connector.finger_box
        keep = connector.keepout_polygon()
        assert min(x for x, _ in keep) < x1
        assert max(x for x, _ in keep) > x2
        assert min(y for _, y in keep) < y1
        assert max(y for _, y in keep) > y2
        del tree

    def test_the_fingers_sit_on_the_declared_board_edge(self, board) -> None:
        tree, netlist = board
        frame = frame_for(netlist)
        assert frame is not None
        extents, _ = component_extents(netlist)
        placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
        connector = edge_connectors(netlist, placement)[0]
        ring = list(frame.polygon())
        worst = 0.0
        for path in connector.paths:
            for point in path:
                worst = max(worst, _distance_to_ring(point, ring))
        assert worst <= EDGE_TOLERANCE_MM, (
            f"the footprint's own edge geometry sits up to {worst:.4f} mm from the "
            "declared outline"
        )
        del tree

    def test_the_pcie_fingers_do_not_share_pad_numbers(self, board) -> None:
        """The M7 lesson, re-verified for the connector M11b actually integrates.

        `docs/roadmap.md` records that pads sharing a *number* share a UUID, and
        the orchestrator's warning was that an edge connector is where that bites
        hardest. Measured: it does not bite here at all. KiCad's PCIe x1 footprint
        numbers all 36 contacts distinctly -- A1..A18 and B1..B18 -- so every
        finger has an identity of its own in the file as well as in the router.
        """
        tree, _ = board
        connector = next(
            fp
            for fp in tree.children("footprint")
            if any(
                p.value(0) == "Reference" and p.value(1) == "J1"
                for p in fp.children("property")
            )
        )
        pads = list(connector.children("pad"))
        numbers = [p.value(0) for p in pads]
        uuids = [p.get("uuid") for p in pads]
        assert len(pads) == 36
        assert len(set(numbers)) == 36
        assert len(set(uuids)) == 36

    def test_the_sata_shells_do_share_one_and_the_router_still_tells_them_apart(
        self, board
    ) -> None:
        """And the case that *does* alias, on the same board, keyed apart anyway.

        The JST connector's two shell tabs are both pad `MP`, so they share a UUID
        exactly as the roadmap says. The router keys on `reference#index` and sees
        two obstacles, which is what M10 predicted and what this asserts on a
        second, independent footprint.
        """
        tree, _ = board
        connector = next(
            fp
            for fp in tree.children("footprint")
            if any(
                p.value(0) == "Reference" and p.value(1) == "J2"
                for p in fp.children("property")
            )
        )
        shells = [p for p in connector.children("pad") if p.value(0) == "MP"]
        assert len(shells) == 2
        assert len({p.get("uuid") for p in shells}) == 1, (
            "the shared-UUID defect is expected to still be there"
        )
        environment = extract_obstacles(tree)
        keys = sorted(k for k in environment.obstacles if k.startswith("J2.MP"))
        assert keys == ["J2.MP", "J2.MP#2"]


def _distance_to_ring(point: tuple[float, float], ring: list) -> float:
    best = float("inf")
    for index in range(len(ring)):
        a, b = ring[index], ring[(index + 1) % len(ring)]
        ax, ay = a
        dx, dy = b[0] - ax, b[1] - ay
        span = dx * dx + dy * dy
        if span <= 0:
            best = min(best, math.dist(point, a))
            continue
        t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / span))
        best = min(best, math.dist(point, (ax + t * dx, ay + t * dy)))
    return best


# ---------------------------------------------------------------------------
# the pair via transition (M11c)
# ---------------------------------------------------------------------------


TRANSITION_BASE = """
name: transition-test
libraries: ["{library}/connectors.yaml"]
net_classes:
  hs:
    trace_width_mm: 0.2
    clearance_mm: 0.15
    via_diameter_mm: 0.4
    via_drill_mm: 0.2
    diff_pair_gap_mm: 0.2
    diff_pair_width_mm: 0.2
    impedance_diff_ohm: 100
    standoff_k: 1.5
    reference: In1.Cu
    prefer_layers: [F.Cu]
nets:
  A_P: {{class: hs, diff_pair: A_N}}
  A_N: {{class: hs, diff_pair: A_P}}
  GND: {{class: ground}}
components:
  J1:
    part: CONN_BRK_1X04
    pins: {{"1": A_P, "2": A_N, "3": GND, "4": GND}}
  J2:
    part: CONN_BRK_1X04
    pins: {{"1": A_P, "2": A_N, "3": GND, "4": GND}}
board:
  outline:
    rect: [40.0, 34.0]
placement:
  J1:
    fixed: {{x: 10.0, y: 31.0, rot: 0}}
    reason: fixed so the transition's geometry is a function of the source alone
  J2:
    fixed: {{x: 10.0, y: 12.0, rot: 0}}
    reason: likewise
layout:
  stackup:
    copper_layers: 4
    thickness_mm: 1.6
    planes:
      - {{layer: In1.Cu, net: GND}}
transitions:
  - pair: [A_P, A_N]
    at: [20.0, 20.0]
    between: [F.Cu, {layer}]
    return_vias: {returns}
    return_within_mm: {within}
    return_net: GND
    via: {{drill: 0.2, diameter: 0.4}}
    reason: the pattern under test
"""


def transition_of(
    write_design,
    tmp_path: Path,
    *,
    layer: str = "B.Cu",
    returns: int = 2,
    within: float = 1.2,
):
    source = TRANSITION_BASE.format(
        layer=layer, returns=returns, within=within, library=LIBRARY
    )
    design = write_design(source)
    report = Report()
    build = build_design(design, out_dir=tmp_path / "out", report=report)
    board = parse(
        next(p for p in build.written if p.suffix == ".kicad_pcb").read_text()
    )
    environment = extract_obstacles(board)
    stack = stack_for(build.netlist.layout)
    fresh = Report()
    result = generate_transitions(board, environment, build.netlist, stack, fresh)
    return result, fresh, build.netlist


@needs_kicad_libraries
class TestPairViaTransition:
    def test_two_signal_vias_at_matched_geometry(self, write_design, tmp_path) -> None:
        result, _, _ = transition_of(write_design, tmp_path)
        signal = [
            via
            for connection in result.connections
            for via in connection.vias
            if via.net in ("A_P", "A_N")
        ]
        assert len(signal) == 2
        assert signal[0].diameter == signal[1].diameter == 0.4
        assert signal[0].drill == signal[1].drill == 0.2
        assert signal[0].from_layer == signal[1].from_layer
        assert signal[0].to_layer == signal[1].to_layer
        event = result.events[0]
        centre = event.at
        first, second = sorted(v.point for v in signal)
        assert math.dist(first, centre) == pytest.approx(
            math.dist(second, centre), abs=1e-6
        ), "the two signal vias must be symmetric about the point the source named"
        assert math.dist(first, second) == pytest.approx(event.via_pitch_mm, abs=1e-6)

    def test_the_via_column_opens_out_from_the_pairs_own_pitch(
        self, write_design, tmp_path
    ) -> None:
        """ADR 0007's objection, answered with arithmetic rather than a refusal."""
        result, _, _ = transition_of(write_design, tmp_path)
        event = result.events[0]
        assert event.pitch_mm == pytest.approx(0.2 + 0.2, abs=1e-6)
        assert event.via_pitch_mm == pytest.approx(0.4 + 0.15, abs=1e-6)
        assert event.via_pitch_mm > event.pitch_mm
        # And what it buys: laminate between two nets, not 0.039 mm of nothing.
        assert event.via_pitch_mm - event.via_diameter_mm == pytest.approx(0.15)

    def test_return_vias_are_placed_within_spec(self, write_design, tmp_path) -> None:
        result, _, _ = transition_of(write_design, tmp_path)
        event = result.events[0]
        assert event.return_asked == 2
        assert event.return_placed == 2
        assert event.return_distance_mm <= event.return_within_mm
        returns = [
            via
            for connection in result.connections
            for via in connection.vias
            if via.net == "GND"
        ]
        assert len(returns) == 2
        for via in returns:
            assert math.dist(via.point, event.at) <= event.return_within_mm

    def test_a_budget_too_tight_places_none_and_says_so(
        self, write_design, tmp_path
    ) -> None:
        result, report, _ = transition_of(write_design, tmp_path, within=0.4)
        assert result.events[0].return_placed == 0
        assert "transition-return-vias" in {d.code for d in report.diagnostics}

    def test_an_outer_to_outer_through_via_has_no_stub(
        self, write_design, tmp_path
    ) -> None:
        result, _, _ = transition_of(write_design, tmp_path, layer="B.Cu")
        assert result.events[0].stub_mm == 0.0
        assert result.events[0].barrel == ("F.Cu", "B.Cu")

    def test_a_transition_to_an_inner_layer_leaves_the_rest_of_the_barrel(
        self, write_design, tmp_path
    ) -> None:
        """Computed from the stackup, not from a table.

        A through via is drilled the whole way whatever the file says its span is,
        so a signal that leaves on In2 abandons the In2-to-B.Cu barrel.
        """
        result, _, netlist = transition_of(write_design, tmp_path, layer="In2.Cu")
        assert netlist.layout is not None
        stackup = netlist.layout.stackup
        expected = stackup.barrel_length_mm("In2.Cu", "B.Cu")
        assert expected > 0
        assert result.events[0].stub_mm == pytest.approx(expected, abs=1e-6)
        assert result.events[0].barrel == ("F.Cu", "B.Cu")
        assert result.events[0].stub_mm > STUB_WARN_MM, (
            "this geometry is meant to be over the reporting threshold"
        )

    def test_the_pair_is_split_into_two_coupled_segments(
        self, write_design, tmp_path
    ) -> None:
        result, _, _ = transition_of(write_design, tmp_path)
        assert len(result.pairs) == 2
        assert {p.layer for p in result.pairs} == {"F.Cu", "B.Cu"}
        assert result.handled == {"A_P", "A_N"}
        for pair in result.pairs:
            assert pair.segment
            assert pair.label() != pair.key()

    def test_the_uuid_space_is_a_function_of_the_source(
        self, write_design, tmp_path
    ) -> None:
        _, _, netlist = transition_of(write_design, tmp_path)
        owned = transition_uuids(netlist)
        assert len(owned) == MAX_TRANSITION_VIAS
        assert transition_uuid(0, 0) in owned
        assert transition_uuid(1, 0) not in owned

    def test_generation_is_deterministic(self, write_design, tmp_path) -> None:
        first, _, _ = transition_of(write_design, tmp_path)
        second, _, _ = transition_of(write_design, tmp_path / "again")
        assert [
            (v.net, v.point, v.name)
            for c in first.connections
            for v in c.vias
        ] == [
            (v.net, v.point, v.name)
            for c in second.connections
            for v in c.vias
        ]


@needs_kicad_libraries
class TestTransitionValidation:
    def _codes(self, write_design, tmp_path, source: str) -> set[str]:
        design = write_design(source)
        report = Report()
        build = build_design(design, out_dir=tmp_path / "out", report=report)
        board = parse(
            next(p for p in build.written if p.suffix == ".kicad_pcb").read_text()
        )
        fresh = Report()
        generate_transitions(
            board,
            extract_obstacles(board),
            build.netlist,
            stack_for(build.netlist.layout),
            fresh,
        )
        return {d.code for d in fresh.diagnostics}

    def test_an_unknown_net_is_an_error(self, write_design, tmp_path) -> None:
        source = TRANSITION_BASE.format(
            layer="B.Cu", returns=2, within=1.2, library=LIBRARY
        ).replace(
            "pair: [A_P, A_N]", "pair: [A_P, NOT_A_NET]"
        )
        assert "transition-unknown-net" in self._codes(write_design, tmp_path, source)

    def test_two_nets_that_are_not_a_pair_are_an_error(
        self, write_design, tmp_path
    ) -> None:
        source = TRANSITION_BASE.format(
            layer="B.Cu", returns=2, within=1.2, library=LIBRARY
        ).replace(
            "pair: [A_P, A_N]", "pair: [A_P, GND]"
        )
        assert "transition-not-a-pair" in self._codes(write_design, tmp_path, source)

    def test_a_layer_the_board_does_not_have_is_an_error(
        self, write_design, tmp_path
    ) -> None:
        source = TRANSITION_BASE.format(
            layer="In6.Cu", returns=2, within=1.2, library=LIBRARY
        )
        assert "transition-unknown-layer" in self._codes(
            write_design, tmp_path, source
        )


# ---------------------------------------------------------------------------
# AC coupling (M11c)
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestAcCoupling:
    def _report(self, source: str, tmp_path: Path) -> Report:
        design = tmp_path / "design.yaml"
        design.write_text(source, encoding="utf-8")
        report = Report()
        netlist = elaborate(load_design(design, report=report), report)
        fresh = Report()
        run_ac_coupling_checks(netlist, fresh)
        return fresh

    def _source(self) -> str:
        return PCIE_SATA.read_text(encoding="utf-8").replace(
            "../library/", str(LIBRARY) + "/"
        )

    def test_the_example_couples_symmetrically(self, tmp_path: Path) -> None:
        report = self._report(self._source(), tmp_path)
        found = [d for d in report.diagnostics if d.code == "ac-coupling"]
        assert len(found) == 1
        assert "0.0000 mm" in found[0].message, found[0].message
        assert not report.warnings, [d.render() for d in report.warnings]

    def test_the_pairing_comes_from_the_netlist(self, tmp_path: Path) -> None:
        design = tmp_path / "design.yaml"
        design.write_text(self._source(), encoding="utf-8")
        report = Report()
        netlist = elaborate(load_design(design, report=report), report)
        frame = frame_for(netlist)
        extents, _ = component_extents(netlist)
        placement = plan_placement(netlist, report=None, extents=extents, frame=frame)
        couplings = ac_couplings(netlist, placement)
        assert len(couplings) == 1
        coupling = couplings[0]
        assert set(coupling.upstream) == {"PCIE_TXP", "PCIE_TXN"}
        assert set(coupling.downstream) == {"PCIE_TXP_C", "PCIE_TXN_C"}
        assert coupling.pitch_mm == pytest.approx(1.4, abs=1e-6)
        assert coupling.skewed_mm == pytest.approx(0.0, abs=1e-9)

    def test_moving_one_capacitor_along_the_route_is_reported(
        self, tmp_path: Path
    ) -> None:
        """Symmetric entry, verified geometrically rather than asserted."""
        source = self._source().replace(
            "fixed: { x: 40.7, y: 14.0, rot: 270 }",
            "fixed: { x: 40.7, y: 14.5, rot: 270 }",
        )
        report = self._report(source, tmp_path)
        found = [d for d in report.warnings if d.code == "ac-coupling-asymmetric"]
        assert found, [d.code for d in report.diagnostics]
        assert "0.500 mm out of line" in found[0].message

    def test_a_lone_coupling_capacitor_is_reported(self, tmp_path: Path) -> None:
        source = self._source().replace(
            """  C_AC_N:
    part: C_100N_0402
    role: ac_coupling""",
            """  C_AC_N:
    part: C_100N_0402
    role: series""",
        )
        report = self._report(source, tmp_path)
        assert "ac-coupling-unpaired" in {d.code for d in report.warnings}


# ---------------------------------------------------------------------------
# the reference board (M11 acceptance)
# ---------------------------------------------------------------------------


_CHECKED: dict[str, tuple] = {}


def checked_board(tmp_path_factory) -> tuple:
    if "pcie-sata" not in _CHECKED:
        target = tmp_path_factory.mktemp("pcie-sata")
        report = Report()
        result = check_design(PCIE_SATA, out_dir=target, report=report)
        _CHECKED["pcie-sata"] = (result, list(report))
    return _CHECKED["pcie-sata"]


@pytest.fixture(scope="module")
def checked(tmp_path_factory: pytest.TempPathFactory) -> tuple:
    return checked_board(tmp_path_factory)


@needs_kicad_cli
@needs_kicad_libraries
class TestReferenceBoard:

    def test_the_pipeline_completes_with_no_errors(self, checked) -> None:
        result, report = checked
        assert result.erc.ran and result.drc.ran
        errors = [d for d in report if d.severity is Severity.ERROR]
        assert not errors, "\n".join(d.render() for d in errors)

    def test_every_pair_is_coupled(self, checked) -> None:
        result, _ = checked
        assert result.routing is not None
        audits = result.routing.pair_audits
        refused = [a for a in audits if not a.coupled]
        assert not refused, [(a.key, a.reason) for a in refused]
        assert len({a.key for a in audits}) == 12, (
            "eleven declared pairs, and the transmit pair counted twice because a "
            "series capacitor splits it into two coupled runs"
        )

    def test_nothing_is_handed_over(self, checked) -> None:
        result, _ = checked
        assert not result.handed_over, result.handed_over

    def test_every_pair_holds_its_derived_geometry(self, checked) -> None:
        result, _ = checked
        assert result.highspeed is not None
        for pair in result.highspeed.pairs:
            assert abs(pair.width_deviation) < 0.01, pair.to_dict()
            assert pair.actual_gap_mm is not None
            assert abs(pair.gap_deviation) < 0.05, pair.to_dict()

    def test_every_pair_is_inside_its_coupling_budget(self, checked) -> None:
        result, _ = checked
        assert result.highspeed is not None
        for pair in result.highspeed.pairs:
            assert pair.budget_mm is not None
            assert max(pair.uncoupled_mm) < pair.budget_mm, pair.to_dict()

    def test_the_reference_plane_is_continuous_under_every_pair(self, checked) -> None:
        result, report = checked
        assert result.highspeed is not None
        assert result.highspeed.reference_checked
        assert result.highspeed.projected_mm > 500
        assert not result.highspeed.gaps, [g.to_dict() for g in result.highspeed.gaps]
        assert "hs-reference-continuous" in {d.code for d in report}

    def test_no_via_stub_reaches_the_threshold(self, checked) -> None:
        result, _ = checked
        assert result.highspeed is not None
        assert result.highspeed.stubs, "the transitions put vias on the lane"
        for stub in result.highspeed.stubs:
            assert stub.stub_mm < STUB_WARN_MM

    def test_the_transitions_got_their_return_vias(self, checked) -> None:
        result, _ = checked
        assert result.routing is not None
        transitions = result.routing.transitions
        assert transitions is not None
        assert len(transitions.events) == 2
        for event in transitions.events:
            assert event.return_placed == event.return_asked == 2

    def test_the_wall_hugging_rule_fires_and_says_what_it_found(self, checked) -> None:
        """M11d rule 2, on the board rather than on a fixture.

        The transmit pair leaves the controller between two ground pads 0.5 mm
        away and runs beside them for longer than five times its gap. Re-tightening
        with their clearance inflated cannot move a pad, so the flag stands -- which
        is the outcome the specification asks for when the re-tighten does not
        resolve it.
        """
        result, report = checked
        assert result.routing is not None
        hugging = [a for a in result.routing.pair_audits if a.wall_hugs]
        assert hugging, "rule 2 found nothing on a board it should find something on"
        assert all(a.retightened for a in hugging)
        assert all(not a.resolved_by_retighten for a in hugging)
        flagged = [d for d in report if d.code == "diff-pair-wall-hugging"]
        assert flagged
        assert "mm for" in flagged[0].message

    def test_the_json_carries_the_highspeed_report(self, checked) -> None:
        result, _ = checked
        payload = json.loads(json.dumps(result.summary()))
        assert "highspeed" in payload
        highspeed = payload["highspeed"]
        assert "not electromagnetic simulation" in highspeed["method"]
        assert highspeed["reference_gaps"] == []
        assert payload["routing"]["transitions"]["transitions"] == 2
        assert len(payload["routing"]["pairs"]) == 14


# ---------------------------------------------------------------------------
# the M11e findings, on boards built to produce them
# ---------------------------------------------------------------------------


#: A pair between two headers, with room around it, and a third header that can be
#: moved in beside it. Small enough to build and route in a couple of seconds,
#: which is what makes it the right place to assert what the M11d rules do.
CROWD_BASE = """
name: crowd-test
libraries: ["{library}/connectors.yaml"]
net_classes:
  hs:
    trace_width_mm: 0.2
    clearance_mm: 0.15
    via_diameter_mm: 0.4
    via_drill_mm: 0.2
    diff_pair_gap_mm: 0.2
{extra}
  ground:
    trace_width_mm: 0.25
    clearance_mm: 0.15
nets:
  A_P: {{class: hs, diff_pair: A_N}}
  A_N: {{class: hs, diff_pair: A_P}}
  GND: {{class: ground}}
  SPARE: {{class: ground}}
components:
  J1:
    part: CONN_BRK_1X04
    pins: {{"1": A_P, "2": A_N, "3": GND, "4": GND}}
  J2:
    part: CONN_BRK_1X04
    pins: {{"1": A_P, "2": A_N, "3": GND, "4": GND}}
  J3:
    part: CONN_PWR_1X02
    pins: {{"1": SPARE, "2": SPARE}}
board:
  outline:
    rect: [40.0, 40.0]
placement:
  J1:
    fixed: {{x: 8.0, y: 34.0, rot: 90}}
    reason: turned so its four pins run across the pair's direction of travel,
      which is what lets the two halves leave it side by side
  J2:
    fixed: {{x: 8.0, y: 14.0, rot: 90}}
    reason: likewise
  J3:
    fixed: {{x: {crowd}, y: 24.0, rot: 0}}
    reason: the wall the pair may or may not hug. Two pads stacked along the
      route, which is four millimetres of something to run beside
layout:
  stackup:
    copper_layers: 4
    thickness_mm: 1.6
    planes:
      - {{layer: In1.Cu, net: GND}}
pours:
  - net: GND
    layers: [In1.Cu]
    scope: board
    reason: the reference the pair is designed against
"""

HS_CLASS = (
    "    impedance_diff_ohm: 100\n"
    "    reference: In1.Cu\n"
    "    prefer_layers: [F.Cu]\n"
    "    coupling: tight\n"
    "    max_uncoupled_mm: 8.0\n"
    "    standoff_k: 1.5\n"
)


def routed_crowd(write_design, tmp_path: Path, *, extra: str, crowd: float):
    """Build and route the crowded fixture, and hand back what M11d measured."""
    source = CROWD_BASE.format(library=LIBRARY, extra=extra, crowd=crowd)
    design = write_design(source)
    report = Report()
    build = build_design(design, out_dir=tmp_path / "out", report=report)
    board_path = next(p for p in build.written if p.suffix == ".kicad_pcb")
    tree = parse(board_path.read_text(encoding="utf-8"))
    fresh = Report()
    routed = route_board(tree, build.netlist, fresh)
    return routed, fresh, build.netlist, tree


@needs_kicad_libraries
class TestStretcherRules:
    """M11d, on a board built so that each rule has something to find."""

    def test_the_standoff_applies_only_to_controlled_impedance_classes(
        self, write_design, tmp_path
    ) -> None:
        plain, _, _, _ = routed_crowd(
            write_design, tmp_path / "plain", extra="", crowd=30.0
        )
        assert plain.pair_audits, "the pair is still routed and still measured"
        for audit in plain.pair_audits:
            assert audit.target_ohm is None
            assert audit.standoff == 1.0, (
                "a class with no impedance target is tightened exactly as it was "
                "before M11: the corridor is the bare clearance"
            )
            assert audit.wall_hugs == ()
            assert audit.budget_mm is None

    def test_a_pair_beside_a_wall_is_re_tightened_away_from_it(
        self, write_design, tmp_path
    ) -> None:
        """Rule 2, resolved. There is room to move, so the pair moves."""
        routed, report, _, _ = routed_crowd(
            write_design, tmp_path, extra=HS_CLASS, crowd=11.07
        )
        audits = [a for a in routed.pair_audits if a.retightened]
        assert audits, "rule 2 never fired on a fixture built for it"
        assert any(a.resolved_by_retighten for a in audits), [
            (a.key, [h.to_dict() for h in a.wall_hugs]) for a in routed.pair_audits
        ]
        assert "diff-pair-wall-hugging" not in {d.code for d in report.diagnostics}

    def test_a_pair_with_room_around_it_is_not_flagged(
        self, write_design, tmp_path
    ) -> None:
        """The rule has to be quiet when there is nothing to be loud about."""
        routed, _, _, _ = routed_crowd(
            write_design, tmp_path, extra=HS_CLASS, crowd=30.0
        )
        assert routed.pair_audits
        assert not any(a.wall_hugs for a in routed.pair_audits)

    def test_an_uncoupled_budget_that_cannot_be_met_is_a_refusal(
        self, write_design, tmp_path
    ) -> None:
        """Rule 3: over budget hands the pair over rather than routing it anyway."""
        tight = HS_CLASS.replace("max_uncoupled_mm: 8.0", "max_uncoupled_mm: 0.05")
        routed, _report, _, _ = routed_crowd(
            write_design, tmp_path, extra=tight, crowd=30.0
        )
        refused = [a for a in routed.pair_audits if not a.coupled]
        assert refused
        assert refused[0].reason == "uncoupled budget exceeded"
        handed = [h for h in routed.handed_over() if h["net"].startswith("A_")]
        assert handed, routed.handed_over()
        assert handed[0]["unrouted"] == "over_complexity"
        assert "mm budget" in handed[0]["reason"]
        assert "mm)" in handed[0]["reason"], "the per-half lengths have to be listed"

    def test_a_standoff_the_board_cannot_give_refuses_and_names_itself(
        self, write_design, tmp_path
    ) -> None:
        """Rule 1's own failure mode, and the hint that explains it."""
        huge = HS_CLASS.replace("standoff_k: 1.5", "standoff_k: 40.0")
        routed, report, _, _ = routed_crowd(
            write_design, tmp_path, extra=huge, crowd=11.07
        )
        refused = [a for a in routed.pair_audits if not a.coupled]
        assert refused, "six millimetres of standoff is not available anywhere here"
        hints = [
            d.hint or ""
            for d in report.diagnostics
            if d.code == "diff-pair-not-coupled"
        ]
        assert any("standoff_k" in hint for hint in hints), hints


@needs_kicad_cli
@needs_kicad_libraries
class TestHighSpeedFindings:
    """The M11e findings, on boards built to produce them."""

    def _check(self, source: str, tmp_path: Path) -> tuple:
        design = tmp_path / "design.yaml"
        design.write_text(source, encoding="utf-8")
        report = Report()
        result = check_design(design, out_dir=tmp_path / "out", report=report)
        return result, list(report)

    def _crowd(self, extra: str, tmp_path: Path, crowd: float = 30.0) -> tuple:
        return self._check(
            CROWD_BASE.format(library=LIBRARY, extra=extra, crowd=crowd), tmp_path
        )

    def test_a_deliberately_wrong_width_is_reported_twice(self, tmp_path) -> None:
        """Once by validation against the target, once by the audit against copper.

        The acceptance test the milestone asks for: an explicit width that
        contradicts the impedance target has to produce *both* the source-level
        warning and the M11e deviation finding, because they are different
        claims -- one about what the source says and one about what got etched.
        """
        extra = HS_CLASS + "    diff_pair_width_mm: 0.25\n"
        result, report = self._crowd(extra, tmp_path)
        codes = {d.code for d in report}
        assert "impedance-geometry-override" in codes, sorted(codes)
        assert "hs-width-deviation" in codes, sorted(codes)
        assert result.highspeed is not None
        pair = result.highspeed.pairs[0]
        assert pair.actual_widths_mm == (0.25,)
        assert pair.target_width_mm == pytest.approx(0.4136, abs=0.002)
        assert pair.width_deviation < -0.3

    def test_a_class_may_promote_its_findings_to_errors(self, tmp_path) -> None:
        extra = HS_CLASS + "    diff_pair_width_mm: 0.25\n    verify: error\n"
        _, report = self._crowd(extra, tmp_path)
        promoted = [
            d
            for d in report
            if d.code == "hs-width-deviation" and d.severity is Severity.ERROR
        ]
        assert promoted, [d.code for d in report if d.code.startswith("hs-")]

    def test_a_split_in_the_reference_plane_is_found_and_located(
        self, tmp_path
    ) -> None:
        """The return-path check, on a plane deliberately cut in two.

        A second pour of a different net takes a strip of the reference layer
        straight across the pair's path. Nothing about the *pair* changes: what
        changes is what is under it, which is the only thing this check looks at.
        """
        source = CROWD_BASE.format(library=LIBRARY, extra=HS_CLASS, crowd=30.0)
        source = source.replace(
            """pours:
  - net: GND
    layers: [In1.Cu]
    scope: board
    reason: the reference the pair is designed against
""",
            """pours:
  - net: GND
    layers: [In1.Cu]
    scope: board
    priority: 0
    reason: the reference the pair is designed against
  - net: SPARE
    layer: In1.Cu
    region:
      rect: [[4.0, 24.0], [20.0, 26.0]]
    priority: 1
    reason: a strip of another net straight across the pair's return path
""",
        )
        result, report = self._check(source, tmp_path)
        assert result.highspeed is not None
        assert result.highspeed.reference_checked
        gaps = result.highspeed.gaps
        assert gaps, "a plane cut in two under the pair was not noticed"
        assert any(g.kind == "split" and g.other_net == "SPARE" for g in gaps), [
            g.to_dict() for g in gaps
        ]
        found = [d for d in report if d.code == "hs-reference-broken"]
        assert found
        assert "In1.Cu" in found[0].message
        assert "SPARE" in found[0].message

    def test_a_via_stub_over_the_threshold_is_reported(self, tmp_path) -> None:
        """A transition that leaves on an inner layer abandons the rest of its barrel."""
        source = CROWD_BASE.format(library=LIBRARY, extra=HS_CLASS, crowd=30.0)
        source = source.replace(
            "pours:",
            """transitions:
  - pair: [A_P, A_N]
    at: [10.0, 25.0]
    between: [F.Cu, In2.Cu]
    return_vias: 2
    return_within_mm: 1.6
    return_net: GND
    via: { drill: 0.2, diameter: 0.4 }
    reason: a transition that stops half way down the board

pours:""",
        )
        result, report = self._check(source, tmp_path)
        assert result.highspeed is not None
        assert result.highspeed.stubs
        worst = result.highspeed.stubs[0]
        assert worst.stub_mm > STUB_WARN_MM
        assert worst.span == ("F.Cu", "B.Cu")
        assert worst.used == ("F.Cu", "In2.Cu")
        codes = {d.code for d in report}
        assert "hs-via-stub" in codes, sorted(codes)


@needs_kicad_cli
@needs_kicad_libraries
class TestGeneratorRegime:
    """The two promises every pattern generator in this toolchain has to keep."""

    def test_a_second_run_into_the_same_directory_is_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """The transition generator strips its own previous vias before it runs.

        Without that, run two would route around run one's copper and the board
        would drift on every invocation -- which is exactly what M10 said the
        stitching generator had to avoid, for the same reason.
        """
        target = tmp_path / "out"
        first = _route_all(target)
        second = _route_all(target)
        assert first == second

    def test_the_transition_vias_are_not_duplicated_by_a_second_run(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "out"
        _route_all(target)
        before = _count_vias(target)
        _route_all(target)
        assert _count_vias(target) == before


def _route_all(target: Path) -> str:
    import subprocess
    import sys

    run = subprocess.run(
        [
            sys.executable, "-m", "aipcb.cli",
            "route", "all", str(PCIE_SATA), "-o", str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return (target / "pcie-sata.kicad_pcb").read_text(encoding="utf-8")


def _count_vias(target: Path) -> int:
    tree = parse((target / "pcie-sata.kicad_pcb").read_text(encoding="utf-8"))
    return len(list(tree.children("via")))


def test_the_report_is_empty_on_a_design_with_no_controlled_impedance() -> None:
    """Backward compatibility, stated as a property rather than assumed."""
    report = Report()
    netlist = elaborate(
        load_design(REPO_ROOT / "examples" / "usb-port" / "design.yaml", report=report),
        report,
    )
    assert controlled_classes(netlist) == {}
    result = analyse_highspeed(parse("(kicad_pcb)"), netlist, [], {})
    assert result.to_dict()["pairs"] == []

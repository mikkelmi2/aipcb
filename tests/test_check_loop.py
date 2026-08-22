"""The check loop: KiCad's findings, re-expressed against the source.

The point of M4 is that a violation KiCad reports against a pad at some coordinate
comes back as a diagnostic pointing at the line of YAML that owns that pad. These
tests check the mapping both in isolation, against synthetic reports, and
end-to-end against the real ``kicad-cli``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from aipcb.checks.kicad_reports import (
    SEVERITY_MAP,
    parse_drc_report,
    parse_erc_report,
)
from aipcb.checks.loop import CheckResult, check_design
from aipcb.checks.mapping import build_index
from aipcb.compile.build import build_design, compile_netlist
from aipcb.diagnostics import Report, Severity
from aipcb.ids import element_uuid
from aipcb.kicad.sexpr import SNode, parse

from .conftest import (
    REPO_ROOT,
    UNROUTABLE_EXAMPLES,
    needs_kicad_cli,
    needs_kicad_libraries,
)


def netlist_for(name: str):
    return compile_netlist(REPO_ROOT / "examples" / name / "design.yaml", Report())


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestUuidIndex:
    def test_components_resolve_to_their_source(self) -> None:
        netlist = netlist_for("usb-port")
        index = build_index(netlist)
        ref = index.lookup(netlist.components["R1"].uuid)
        assert ref is not None
        assert ref.kind == "component"
        assert ref.label == "R1"
        assert ref.path == ("components", "R1")
        assert ref.loc is not None and ref.loc.line > 0

    def test_pads_resolve_to_the_pin_that_owns_them(self) -> None:
        netlist = netlist_for("usb-port")
        index = build_index(netlist)
        component = netlist.components["R1"]
        ref = index.lookup(element_uuid("fp", *component.hier, "pad", "1"))
        assert ref is not None
        assert ref.kind == "pad"
        assert ref.path == ("components", "R1", "pins", "1")
        assert ref.net == component.connections["1"]

    def test_nets_resolve_by_name(self) -> None:
        index = build_index(netlist_for("usb-port"))
        ref = index.net("USB_DP")
        assert ref is not None and ref.kind == "net"

    def test_sheet_prefixed_net_names_still_resolve(self) -> None:
        """KiCad prefixes sheet-local net names; the index tolerates both forms."""
        index = build_index(netlist_for("usb-port"))
        assert index.net("/USB_DP") is not None

    def test_unknown_uuid_resolves_to_nothing(self) -> None:
        index = build_index(netlist_for("usb-port"))
        assert index.lookup("00000000-0000-0000-0000-000000000000") is None

    @needs_kicad_cli
    def test_index_covers_every_uuid_in_the_generated_files(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """Anything we emit must be traceable back, or M4's mapping has holes.

        Failures here are the interesting case: an unmapped UUID in our own output
        means an emitter grew an element the index does not know about.
        """
        result = build_design(example_design, out_dir=tmp_path)
        index = build_index(result.netlist)

        def uuids(node: SNode) -> list[str]:
            found: list[str] = []
            for child in node.children():
                if child.name == "uuid" and child.value(0):
                    found.append(str(child.value(0)))
                found.extend(uuids(child))
            return found

        unmapped: list[str] = []
        for path in result.written:
            if path.suffix not in (".kicad_sch", ".kicad_pcb"):
                continue
            root = parse(path.read_text(encoding="utf-8"))
            # The sheet's own uuid is the document's, not an element's.
            document_uuid = root.get("uuid")
            for value in uuids(root):
                if value != document_uuid and index.lookup(value) is None:
                    unmapped.append(f"{path.name}: {value}")

        assert not unmapped, (
            f"{len(unmapped)} generated UUIDs cannot be mapped back to source:\n"
            + "\n".join(unmapped[:10])
        )


# ---------------------------------------------------------------------------
# parsing, without needing KiCad
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestReportParsing:
    def _index_and_component(self):
        netlist = netlist_for("usb-port")
        return build_index(netlist), netlist.components["R1"]

    def test_erc_violation_lands_on_the_source_line(self) -> None:
        index, component = self._index_and_component()
        payload = {
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "description": "Pin not connected",
                            "severity": "error",
                            "type": "pin_not_connected",
                            "items": [
                                {
                                    "description": "Symbol R1 Pin 1",
                                    "uuid": element_uuid("pin", *component.hier, "1"),
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        diag = parse_erc_report(payload, index)[0]
        assert diag.severity is Severity.ERROR
        assert diag.code == "kicad-pin-not-connected"
        assert diag.path == ("components", "R1", "pins", "1")
        assert diag.loc is not None
        assert diag.context["component"] == "R1"
        assert "R1 pin 1" in diag.message

    def test_drc_splits_its_three_lists(self) -> None:
        index, component = self._index_and_component()
        item = {"uuid": element_uuid("fp", *component.hier, "pad", "1")}
        payload = {
            "violations": [
                {"type": "clearance", "severity": "error",
                 "description": "Clearance violation", "items": [item]}
            ],
            "unconnected_items": [
                {"type": "unconnected_items", "severity": "error",
                 "description": "Missing connection", "items": [item]}
            ],
            "schematic_parity": [
                {"type": "net_conflict", "severity": "warning",
                 "description": "Pad net mismatch", "items": [item]}
            ],
        }
        origins = {d.context["origin"] for d in parse_drc_report(payload, index)}
        assert origins == {"drc", "drc-unconnected", "drc-parity"}

    def test_unrouted_nets_are_notes_not_errors(self) -> None:
        """Ratlines are the expected state before routing, so they must not shout."""
        index, component = self._index_and_component()
        payload = {
            "unconnected_items": [
                {
                    "type": "unconnected_items",
                    "severity": "error",
                    "description": "Missing connection",
                    "items": [{"uuid": element_uuid("fp", *component.hier, "pad", "1")}],
                }
            ]
        }
        diag = parse_drc_report(payload, index)[0]
        assert diag.severity is Severity.INFO

    def test_hand_edited_items_say_so(self) -> None:
        """A UUID we never emitted came from a human editing the board in KiCad."""
        index, _ = self._index_and_component()
        payload = {
            "violations": [
                {
                    "type": "clearance",
                    "severity": "error",
                    "description": "Clearance violation",
                    "items": [{"uuid": "11111111-2222-3333-4444-555555555555"}],
                }
            ]
        }
        diag = parse_drc_report(payload, index)[0]
        assert diag.loc is None
        assert "added by hand" in (diag.hint or "")
        assert diag.context["unmapped_uuids"] == ["11111111-2222-3333-4444-555555555555"]

    def test_exclusions_become_notes_rather_than_disappearing(self) -> None:
        """A rule silenced in the GUI is invisible from source; say so, don't drop it."""
        assert SEVERITY_MAP["exclusion"] is Severity.INFO

    def test_empty_report_produces_nothing(self) -> None:
        index, _ = self._index_and_component()
        assert parse_drc_report({}, index) == []
        assert parse_erc_report({"sheets": []}, index) == []


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


#: One check run per example, shared by the tests that read it. A check now builds,
#: routes and runs both of KiCad's checkers, which is a few seconds a design; doing
#: it once per example rather than once per assertion is the difference between a
#: test suite people run and one they skip.
_CHECKED: dict[Path, tuple[CheckResult, Report]] = {}


def checked(design: Path, tmp_path: Path) -> tuple[CheckResult, Report]:
    if design not in _CHECKED:
        report = Report()
        result = check_design(design, out_dir=tmp_path, report=report)
        _CHECKED[design] = (result, report)
    return _CHECKED[design]


@needs_kicad_libraries
@needs_kicad_cli
class TestCheckLoop:
    #: Warnings that are the toolchain refusing rather than something being wrong.
    #: M7d will not fake a coupled differential pair when the end pads are not at
    #: the pair's own pitch; it routes the halves separately and says so. Two of the
    #: bundled examples are exactly that case, and silencing the message would be
    #: worse than living with it.
    HONEST_REFUSALS = frozenset({"diff-pair-not-coupled"})

    #: Named, per-example, and deliberately not a blanket allowance. `diff-pair`
    #: ends up with one copper sliver: two GND tracks of the same net diverge at a
    #: shallow angle and leave a wedge a few microns wide, which KiCad reports and a
    #: fabricator would rather not etch. It is a limitation of the same-net trimming
    #: heuristic, not of this milestone, and it is recorded in docs/roadmap.md.
    #: Every other example, on every other rule, must still come back clean.
    #: `qfn-fanout` sets `min_contiguous: 0.9` on a board whose 0.5 mm-pitch
    #: escape field genuinely cuts the back plane into pieces, so the
    #: fragmentation warning fires on purpose. It is the M10d acceptance case
    #: living in an example rather than only in a fixture; lowering the threshold
    #: to silence it would be tuning the question until the answer was yes.
    KNOWN_ISSUES: ClassVar[dict[str, frozenset[str]]] = {
        "diff-pair": frozenset({"kicad-copper-sliver"}),
        "qfn-fanout": frozenset({"plane-fragmented"}),
        # Three codes, and none of them is the board being wrong. They are what
        # M11 built, doing what it was built to do, on the one board dense enough
        # to make it speak:
        #
        # * `diff-pair-wall-hugging` -- M11d rule 2 on the three pairs that leave
        #   the controller between two ground pads 0.5 mm away and run beside them
        #   for longer than five times their gap. Re-tightening with the pads'
        #   clearance inflated cannot move a pad, so the flag stands, which is the
        #   outcome the specification names.
        # * `diff-pair-skew` and `hs-skew` -- the receive pair and the reference
        #   clock come out 0.25-0.29 mm out of length, against the 0.125 mm their
        #   class declares. The via transition is where it comes from and there is
        #   no fan-out long enough to meander it away. It is inside what PCIe Gen3
        #   allows within a pair and outside the house rule the class asks for, and
        #   the number is in the report rather than the budget being widened until
        #   it fits.
        # * `kicad-lib-footprint-mismatch` -- the price of ADR 0010's decision that
        #   the board outline has one author: the card-edge footprint is placed
        #   without its own `Edge.Cuts`, so it is no longer byte-identical to its
        #   library copy, and KiCad is right to say so.
        "pcie-sata": frozenset(
            {
                "diff-pair-wall-hugging",
                "diff-pair-skew",
                "hs-skew",
                "kicad-lib-footprint-mismatch",
                # Newly visible in M13.5, and correct. `footprint_filters_mismatch`
                # is `ignore` by default in KiCad 9.0.8; pinning it to `warning`
                # surfaced exactly one finding across all eleven examples, and it is
                # this one: U1's *symbol* is a connector stand-in, because KiCad
                # ships none for a PCIe-to-SATA controller, and its footprint
                # filters therefore say `Connector*:*_1x??_*` while the part
                # declares a QFN-48. The example's own header says the symbols are
                # stand-ins; this is KiCad saying the same thing, and it is the only
                # thing four unpinned rules were hiding.
                "kicad-footprint-filters-mismatch",
            }
        ),
    }

    def test_examples_check_clean(self, example_design: Path, tmp_path: Path) -> None:
        result, report = checked(example_design, tmp_path)
        assert result.erc.ran and result.drc.ran
        allowed = self.HONEST_REFUSALS | self.KNOWN_ISSUES.get(
            example_design.parent.name, frozenset()
        )
        if example_design.parent.name in UNROUTABLE_EXAMPLES:
            allowed = allowed | {"route-handed-over", "kicad-unconnected-items"}
        problems = [
            d
            for d in report
            if d.severity is not Severity.INFO and d.code not in allowed
        ]
        assert not problems, "\n".join(d.render() for d in problems)

    def test_nothing_is_left_unconnected(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """A checked board is a routed board, so no ratline should survive it.

        This is the acceptance bar in one assertion: every example routes to
        completion, nothing is handed over, and KiCad agrees that every pad is
        joined to the net the source put it on.
        """
        result, report = checked(example_design, tmp_path)
        if example_design.parent.name in UNROUTABLE_EXAMPLES:
            pytest.skip("this example exists to be unroutable")
        assert not result.handed_over, result.handed_over
        unconnected = [d for d in report if d.code == "kicad-unconnected-items"]
        assert not unconnected, "\n".join(d.render() for d in unconnected)

    def test_a_real_violation_maps_to_its_source_line(self, tmp_path: Path) -> None:
        """The end-to-end claim: break the source, get pointed back at the break."""
        source = (REPO_ROOT / "examples" / "usb-port" / "design.yaml").read_text()
        # A clearance no layout could satisfy, so DRC has to complain about pads.
        broken = source.replace(
            "    trace_width_mm: 0.25\n    clearance_mm: 0.2\n    diff_pair_width_mm",
            "    trace_width_mm: 0.25\n    clearance_mm: 2.5\n    diff_pair_width_mm",
        )
        assert broken != source
        # Keep the same directory shape as examples/, so `../library/…` resolves.
        (tmp_path / "library").symlink_to(REPO_ROOT / "examples" / "library")
        (tmp_path / "usb-port").mkdir()
        design = tmp_path / "usb-port" / "design.yaml"
        design.write_text(broken, encoding="utf-8")

        report = Report()
        check_design(design, out_dir=tmp_path / "out", report=report)
        clearance = [d for d in report if d.code == "kicad-clearance"]
        assert clearance, report.render()
        diag = clearance[0]
        assert diag.severity is Severity.ERROR
        assert diag.loc is not None and diag.loc.file == design
        assert diag.path[0] == "components"
        assert "net_classes" in (diag.hint or "")

    def test_check_leaves_no_artefacts_when_no_out_given(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        before = set(design.parent.iterdir())
        check_design(design, report=Report())
        assert set(design.parent.iterdir()) == before


@needs_kicad_libraries
@needs_kicad_cli
class TestCheckCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "check", *args],
            capture_output=True, text=True, check=False,
        )

    def test_exit_zero_on_a_clean_design(self, example_design: Path) -> None:
        # `--no-route` for the same reason as below: whether every example checks
        # clean *with* its copper is what `TestCheckLoop` asserts, once per example.
        # This is about the exit code.
        result = self._run(str(example_design), "--no-route")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_cli_routes_before_it_checks(self, tmp_path: Path) -> None:
        """The default is a checked board with copper on it, and it says so."""
        result = self._run(
            str(REPO_ROOT / "examples" / "led-blinker" / "design.yaml"), "--json"
        )
        payload = json.loads(result.stdout)
        routing = payload["summary"]["routing"]
        assert routing["routed"] > 0
        assert routing["handed_over"] == []

    def test_json_carries_the_summary(self, example_design: Path) -> None:
        # `--no-route` because this is about the shape of the report, not about the
        # copper: routing every example three more times over is minutes of test
        # suite for an assertion that does not read the tracks.
        result = self._run(str(example_design), "--json", "--no-route")
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["summary"]["erc"]["ran"] is True
        assert payload["summary"]["drc"]["ran"] is True
        for diagnostic in payload["diagnostics"]:
            assert "severity" in diagnostic and "code" in diagnostic

    def test_checks_can_be_skipped(self, example_design: Path) -> None:
        payload = json.loads(
            self._run(str(example_design), "--json", "--no-drc", "--no-route").stdout
        )
        assert payload["summary"]["erc"]["ran"] is True
        assert payload["summary"]["drc"]["ran"] is False
        assert "routing" not in payload["summary"]

    def test_unreadable_input_exits_two(self, tmp_path: Path) -> None:
        assert self._run(str(tmp_path / "nope.yaml")).returncode == 2


# ---------------------------------------------------------------------------
# what DRC can and cannot see (M13.5)
# ---------------------------------------------------------------------------


@needs_kicad_cli
@needs_kicad_libraries
class TestNothingIsSilentlyFiltered:
    """M13a fixed a router defect that produced two `tracks_crossing` violations.

    M13.5 asked the verification-integrity question behind it: *why did `check`
    never say so?* The answer turned out to be that it does -- the crossing only
    ever existed on a board carrying a strap the shipped example leaves off, so no
    board `check` ran on had one. These tests hold that answer in place, because
    "the pipeline can see it" is exactly the kind of claim that rots quietly.

    What the same investigation did find is one level down, in `kicad-cli` rather
    than here: ``--severity-all`` means error, warning and exclusion, and *not*
    ``ignore``, so a rule KiCad defaults to ``ignore`` never reaches us and the
    report does not admit that a category was dropped. See
    :data:`aipcb.compile.project.DRC_SEVERITIES`.
    """

    def _built(self, tmp_path: Path, name: str = "routing-demo") -> tuple[Path, Path]:
        report = Report()
        result = build_design(
            REPO_ROOT / "examples" / name / "design.yaml",
            out_dir=tmp_path / "out",
            report=report,
        )
        board = next(p for p in result.written if p.suffix == ".kicad_pcb")
        return board, result.netlist

    def _drc(self, board: Path, netlist, tmp_path: Path) -> Report:
        from aipcb.checks.kicad_reports import run_drc

        report = Report()
        run_drc(board, build_index(netlist), report, work=tmp_path)
        return report

    def test_two_crossing_tracks_are_reported_as_an_error(self, tmp_path: Path) -> None:
        """The milestone's own test: lay copper of two nets in one place.

        The tracks go on `routing-demo`, inside its outline, on one layer, at right
        angles. Nothing subtle -- the point is that the *whole* path from board file
        to diagnostic carries the category through at error severity.
        """
        board, netlist = self._built(tmp_path)
        _cross(board, at=(135.0, 122.5))
        report = self._drc(board, netlist, tmp_path)

        crossing = [d for d in report if d.code == "kicad-tracks-crossing"]
        assert crossing, [f"{d.severity.value} {d.code}" for d in report]
        assert crossing[0].severity is Severity.ERROR
        assert crossing[0].hint  # and it says what to do about it

    def test_copper_outside_the_outline_is_invisible_to_drc(
        self, tmp_path: Path
    ) -> None:
        """The same two tracks, off the board, are reported by nothing.

        Measured on KiCad 9.0.8: copper outside `Edge.Cuts` gets no
        `tracks_crossing`, no `clearance` and no `copper_edge_clearance` -- only
        `track_dangling`, which is about its ends rather than about where it is.
        Nothing in aipcb puts copper there, and this is here so that a change which
        starts to would not be checked by a DRC that cannot see it.
        """
        board, netlist = self._built(tmp_path)
        _cross(board, at=(20.0, 20.0))  # outline is 100..170 x 100..145
        codes = {d.code for d in self._drc(board, netlist, tmp_path)}
        assert "kicad-tracks-crossing" not in codes
        assert "kicad-clearance" not in codes

    def test_a_footprint_without_a_courtyard_is_reported(self, tmp_path: Path) -> None:
        """The gap that *was* real, and is closed.

        `missing_courtyard` is `ignore` in KiCad 9.0.8, so a footprint with no
        courtyard silently opted out of `courtyards_overlap` -- the rule that says
        whether a placement is legal. The project file now pins it.
        """
        board, netlist = self._built(tmp_path, "led-blinker")
        board.write_text(
            board.read_text(encoding="utf-8").replace('"F.CrtYd"', '"F.Fab"'),
            encoding="utf-8",
        )
        codes = [d.code for d in self._drc(board, netlist, tmp_path)]
        assert "kicad-missing-courtyard" in codes

    def test_the_project_pins_the_rules_kicad_would_ignore(
        self, tmp_path: Path
    ) -> None:
        """Every generated project names a severity for each rule KiCad silences.

        `--severity-all` does not include `ignore`, so this is the only place the
        decision can be made. Asserting it on a design that needs no rule changes
        at all is deliberate: the pins are unconditional.
        """
        from aipcb.compile.project import DRC_SEVERITIES

        build_design(REPO_ROOT / "examples" / "usb-port" / "design.yaml", out_dir=tmp_path)
        project = json.loads(
            next(tmp_path.glob("*.kicad_pro")).read_text(encoding="utf-8")
        )
        assert project["board"]["design_settings"]["rule_severities"] == DRC_SEVERITIES
        assert "ignore" not in set(DRC_SEVERITIES.values())

    def test_every_hint_names_a_rule_kicad_actually_has(self) -> None:
        """A hint keyed on a misspelt rule is a hint nobody ever sees.

        `courtyard_overlap` was spelt singular from M4 to M13.5 and KiCad's rule is
        `courtyards_overlap`, so it never fired once. The rule names below are the
        ones KiCad 9.0.8 writes into its own shipped project templates.
        """
        from aipcb.checks.kicad_reports import _HINTS

        # ERC rules live in a different namespace from DRC's and are checked by
        # `TestErcReport` instead; these are the board ones.
        erc_only = {"power_pin_not_driven", "pin_not_connected"}
        for rule in set(_HINTS) - erc_only:
            assert rule in KICAD_DRC_RULES, f"{rule} is not a KiCad DRC rule name"


#: KiCad 9.0.8's DRC rule names, read off the `rule_severities` maps in the project
#: templates and demos KiCad ships (37 of them, 60 distinct rules). Written down
#: rather than derived at test time so the list is reviewable, and dated so the next
#: person can tell how old it is -- ADR 0009's rule, applied to a different tool
#: surface.
KICAD_DRC_RULES = frozenset({
    "annular_width", "clearance", "connection_width", "copper_edge_clearance",
    "copper_sliver", "courtyards_overlap", "creepage", "diff_pair_gap_out_of_range",
    "diff_pair_uncoupled_length_too_long", "drill_out_of_range",
    "duplicate_footprints", "extra_footprint", "footprint",
    "footprint_filters_mismatch", "footprint_symbol_mismatch",
    "footprint_type_mismatch", "hole_clearance", "hole_near_hole", "hole_to_hole",
    "holes_co_located", "invalid_outline", "isolated_copper",
    "item_on_disabled_layer", "items_not_allowed", "length_out_of_range",
    "lib_footprint_issues", "lib_footprint_mismatch", "malformed_courtyard",
    "microvia_drill_out_of_range", "mirrored_text_on_front_layer",
    "missing_courtyard", "missing_footprint", "net_conflict",
    "nonmirrored_text_on_back_layer", "npth_inside_courtyard", "overlapping_pads",
    "padstack", "pth_inside_courtyard", "shorting_items", "silk_edge_clearance",
    "silk_over_copper", "silk_overlap", "skew_out_of_range", "solder_mask_bridge",
    "starved_thermal", "text_height", "text_on_edge_cuts", "text_thickness",
    "through_hole_pad_without_hole", "too_many_vias", "track_angle",
    "track_dangling", "track_segment_length", "track_width", "tracks_crossing",
    "unconnected_items", "unresolved_variable", "via_dangling",
    "zone_has_empty_net", "zones_intersect",
})


def _cross(board: Path, *, at: tuple[float, float]) -> None:
    """Lay two tracks of different nets across each other, centred on ``at``."""
    x, y = at
    segment = (
        '\t(segment\n\t\t(start {sx} {sy})\n\t\t(end {ex} {ey})\n\t\t(width 0.25)\n'
        '\t\t(layer "F.Cu")\n\t\t(net {net})\n\t\t(uuid "{uuid}")\n\t)\n'
    )
    copper = segment.format(
        sx=x - 3, sy=y, ex=x + 3, ey=y, net=1,
        uuid="aaaaaaaa-0000-4000-8000-000000000001",
    ) + segment.format(
        sx=x, sy=y - 3, ex=x, ey=y + 3, net=3,
        uuid="aaaaaaaa-0000-4000-8000-000000000002",
    )
    text = board.read_text(encoding="utf-8")
    close = text.rstrip().rfind(")")
    board.write_text(text[:close] + copper + text[close:], encoding="utf-8")


@needs_kicad_cli
@needs_kicad_libraries
class TestCheckIsAFunctionOfTheSource:
    """Checking twice into one directory must give the same answer twice.

    It did not. `check` routes the board it builds and writes the copper into it;
    the next build read that copper back as a human's hand routing, kept it, and
    routed the board again on top of it. Copper accumulated, connections started
    failing, and a clearance error appeared out of nothing -- on a design nobody had
    touched between the two runs.
    """

    def test_checking_three_times_changes_nothing(self, tmp_path: Path) -> None:
        """Three runs, not two, and every number the accumulation moved.

        Two runs is enough to catch the bug as it was; three is what says the
        answer is *stationary* rather than merely equal on the first repeat. The
        M13.5 measurement drifted further on every run -- 1108 mm, 1853 mm,
        2462 mm -- so a fix that stopped only the first drift would have passed a
        two-run test and still been wrong.
        """
        design = REPO_ROOT / "examples" / "usb-port" / "design.yaml"
        board = tmp_path / "usb-port.kicad_pcb"

        runs = []
        for _ in range(3):
            result = check_design(design, out_dir=tmp_path, report=Report())
            assert result.routing is not None
            runs.append(
                {
                    "board": board.read_bytes(),
                    "copper_mm": round(result.routing.total_length, 3),
                    "routed": len(result.routing.connections),
                    "failed": len(result.routing.failed),
                    "vias": len(result.routing.vias),
                    "routing": result.routing.summary(),
                    "drc": result.drc.counts,
                    "erc": result.erc.counts,
                }
            )

        first = runs[0]
        for index, run in enumerate(runs[1:], start=2):
            assert run["board"] == first["board"], (
                f"check {index} wrote a different board than check 1"
            )
            for key in ("copper_mm", "routed", "failed", "vias", "routing", "drc", "erc"):
                assert run[key] == first[key], (
                    f"check {index} disagrees with check 1 about {key}: "
                    f"{run[key]!r} against {first[key]!r}"
                )

    def test_checking_the_flagship_board_three_times_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The same invariant on the board the bug was found on.

        `usb-port` above is the cheap version; `pcie-sata` is where M13.5 measured
        the drift, and it is the only example carrying both routed copper and
        declared zones -- the two item kinds `preserve.py` calls a human's. Skipped
        with the rest of the slow corpus unless asked for, because it is 45 s a run.
        """
        if not self._slow_enabled():
            pytest.skip("set AIPCB_FULL_CORPUS=1 to check the flagship board")
        design = REPO_ROOT / "examples" / "pcie-sata" / "design.yaml"
        board = tmp_path / "pcie-sata.kicad_pcb"

        seen = set()
        for _ in range(3):
            result = check_design(design, out_dir=tmp_path, report=Report())
            assert result.routing is not None
            seen.add(
                (
                    board.read_bytes(),
                    round(result.routing.total_length, 3),
                    len(result.routing.connections),
                    tuple(sorted(result.drc.counts.items())),
                )
            )
        assert len(seen) == 1, "three checks of one design gave more than one answer"

    @staticmethod
    def _slow_enabled() -> bool:
        import os

        return os.environ.get("AIPCB_FULL_CORPUS") == "1"

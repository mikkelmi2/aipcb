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

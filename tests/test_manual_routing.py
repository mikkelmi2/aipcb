"""`routing: manual`, the schematic-edit policy, and the external-router bridge.

Three M14 features that share one theme: saying out loud what is generated and what
is not, instead of leaving it to be inferred.

The bridge tests skip when ``pcbnew`` cannot be imported, and none of them runs an
external router: the point of the bridge is that aipcb never does. A session file is
hand-written where one is needed, which also makes the tests independent of any
particular router's output.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.preserve import (
    SHEET_STAMP_PREFIX,
    schematic_was_edited,
    stamp_schematic,
)
from aipcb.compile.schematic import build_schematic
from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import dump, parse
from aipcb.route.bridge import splice_session_copper, verify_against_source
from aipcb.route.manual import (
    is_manual,
    manual_nets,
    net_routing_mode,
    nets_with_copper,
    routing_states,
)

from .conftest import REPO_ROOT, needs_kicad_libraries

EXAMPLES = REPO_ROOT / "examples"


def has_pcbnew() -> bool:
    from aipcb.kicad.fill import find_pcbnew_python

    return find_pcbnew_python() is not None


needs_pcbnew = pytest.mark.skipif(
    not has_pcbnew(),
    reason="no interpreter on this machine can import pcbnew, which is the only "
    "headless route to Specctra DSN/SES on KiCad 9 (ADR 0006, M14e amendment)",
)


MANUAL_DESIGN = """
name: manual-demo
revision: A
libraries: [../library/passives.yaml, ../library/connectors.yaml]
net_classes:
  quiet:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    routing: manual
    description: Drawn by hand, never by the router.
nets:
  VCC: {class: power, voltage: 3.3}
  GND: {class: ground}
  SIG_A:
    class: quiet
    description: Inherits the class's manual routing.
  SIG_B:
    class: quiet
    routing: auto
    description: Overrides the class the other way, which is the case that proves
      the override is real rather than decorative.
components:
  J1:
    part: CONN_BRK_1X04
    role: connector
    pins: {"1": VCC, "2": SIG_A, "3": SIG_B, "4": GND}
  R1:
    part: R_10K_0603
    role: pull_up
    for: J1
    pins: {"1": VCC, "2": SIG_A}
  R2:
    part: R_10K_0603
    role: pull_up
    for: J1
    pins: {"1": VCC, "2": SIG_B}
  C1:
    part: C_100N_0603
    role: decoupling
    for: J1
    reason: Gives GND a second pin, so the header's ground is a net rather than a
      dangling pin.
    pins: {"1": VCC, "2": GND}
"""


@pytest.fixture
def manual_design(tmp_path: Path) -> Path:
    """A design whose class is manual and whose one net opts back out of it."""
    import shutil

    shutil.copytree(EXAMPLES / "library", tmp_path / "library")
    project = tmp_path / "d"
    project.mkdir()
    path = project / "design.yaml"
    path.write_text(MANUAL_DESIGN, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# M14d -- the declaration
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestManualDeclaration:
    def test_a_class_declares_it_and_a_net_can_override(self, manual_design: Path) -> None:
        netlist = compile_netlist(manual_design, Report())
        assert net_routing_mode(netlist, "SIG_A") == "manual"
        assert net_routing_mode(netlist, "SIG_B") == "auto", (
            "a net's own `routing:` has to beat its class's, or the field is a "
            "class-level switch wearing a net-level name"
        )
        assert manual_nets(netlist) == ["SIG_A"]
        assert not is_manual(netlist, "VCC")

    def test_the_router_leaves_a_declared_manual_net_alone(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        from aipcb.route.pipeline import route_design

        report = Report()
        done = route_design(manual_design, tmp_path / "out", report)
        routed = {c.net for c in done.routed.connections}
        assert "SIG_A" not in routed, "the router touched a net declared manual"
        assert "SIG_B" in routed, "the auto override did not reach the router"
        assert done.routed.summary()["manual"] == ["SIG_A"]

    def test_the_four_states_are_distinguished(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        """manual-pending is the state that would otherwise pass for routed."""
        from aipcb.route.pipeline import route_design

        report = Report()
        done = route_design(manual_design, tmp_path / "out", report)
        states = routing_states(
            done.board,
            done.build.netlist,
            auto_routed={c.net for c in done.routed.connections},
        )
        by_net = {n.net: n.state for n in states.nets}
        assert by_net["SIG_A"] == "manual-pending"
        assert by_net["SIG_B"] == "auto-routed"
        assert [n.net for n in states.pending] == ["SIG_A"]
        assert "manual-pending" in states.counts()

    def test_check_warns_about_a_pending_net(self, manual_design: Path) -> None:
        from aipcb.checks.loop import check_design

        report = Report()
        check_design(manual_design, report=report, schematic=False, board=False)
        codes = [w.code for w in report.warnings]
        assert "routing-manual-pending" in codes

    def test_hand_drawn_copper_moves_it_to_manual_routed(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        """The round trip's last leg: copper appears, and the state follows it.

        The copper is added the way a person would -- straight into the board file,
        with a UUID aipcb has never seen -- because that is exactly what a hand route
        in KiCad and an imported session file both look like from here.
        """
        from aipcb.route.pipeline import route_design

        report = Report()
        done = route_design(manual_design, tmp_path / "out", report)
        board = done.board
        code = next(
            n.value(0) for n in board.children("net") if n.value(1) == "SIG_A"
        )
        from aipcb.kicad.sexpr import SNode, num, quoted, sym

        board.add(
            SNode("segment").add(
                SNode("start").add(num(10.0), num(10.0)),
                SNode("end").add(num(20.0), num(10.0)),
                SNode("width").add(num(0.25)),
                SNode("layer").add(quoted("F.Cu")),
                SNode("net").add(sym(code)),
                SNode("uuid").add(quoted("11111111-2222-3333-4444-555555555555")),
            )
        )
        assert "SIG_A" in nets_with_copper(board, done.build.netlist)
        states = routing_states(board, done.build.netlist)
        assert {n.net: n.state for n in states.nets}["SIG_A"] == "manual-routed"
        assert not states.pending

    def test_a_declared_pattern_still_runs_on_a_manual_net_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """A pattern generator is not the router, and both declarations are explicit.

        `fanout:` asks for a specific escape shape on a specific package. Suppressing
        it because a net is declared manual would silently disable the block the user
        wrote; laying it without a word would surprise somebody who declared the net
        manual in order to draw it themselves. So it runs, and it is reported.
        """
        import shutil

        from aipcb.route.pipeline import route_design

        shutil.copytree(EXAMPLES / "library", tmp_path / "library")
        project = tmp_path / "d"
        shutil.copytree(EXAMPLES / "qfn-fanout", project)
        design = project / "design.yaml"
        lines = design.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith("  IO_PD0:"):
                lines.insert(index + 1, "    routing: manual\n")
                break
        else:  # pragma: no cover - the example declares this net
            pytest.fail("qfn-fanout no longer declares IO_PD0")
        design.write_text("".join(lines), encoding="utf-8")

        report = Report()
        done = route_design(design, tmp_path / "out", report)
        assert "IO_PD0" in done.routed.summary()["manual"]
        codes = [d.code for d in report.diagnostics]
        assert "manual-net-has-generated-pattern" in codes
        # Said once, not once per internal routing pass.
        assert codes.count("manual-net-has-generated-pattern") == 1

    def test_a_controlled_impedance_pending_net_is_warned_about(
        self, tmp_path: Path
    ) -> None:
        """The one rule with teeth: never hand a derived pair geometry to a stranger."""
        import shutil

        from aipcb.route.bridge import export_for_router

        shutil.copytree(EXAMPLES / "library", tmp_path / "library")
        project = tmp_path / "d"
        shutil.copytree(EXAMPLES / "pcie-sata", project)
        design = project / "design.yaml"
        text = design.read_text(encoding="utf-8")
        text = text.replace(
            "  pcie_tx:\n    trace_width_mm: 0.2",
            "  pcie_tx:\n    routing: manual\n    trace_width_mm: 0.2",
            1,
        )
        design.write_text(text, encoding="utf-8")

        netlist = compile_netlist(design, Report())
        build_design(design, out_dir=tmp_path / "out", report=Report())
        board = next((tmp_path / "out").glob("*.kicad_pcb"))

        report = Report()
        with contextlib.suppress(Exception):
            # The warning is raised before the export is attempted, on purpose:
            # a machine with no pcbnew still gets told what it was about to do.
            export_for_router(board, tmp_path / "out" / "x.dsn", netlist, report)
        codes = [w.code for w in report.warnings]
        assert "controlled-impedance-to-external-router" in codes


# ---------------------------------------------------------------------------
# M14c -- the schematic is a view
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestSchematicEditPolicy:
    def test_a_generated_sheet_carries_its_own_hash(self, tmp_path: Path) -> None:
        result = build_design(
            EXAMPLES / "led-blinker" / "design.yaml", out_dir=tmp_path, report=Report()
        )
        sheet = next(p for p in result.written if p.suffix == ".kicad_sch")
        root = parse(sheet.read_text(encoding="utf-8"))
        block = root.child("title_block")
        assert block is not None
        stamps = [
            c.value(1)
            for c in block.children("comment")
            if (c.value(1) or "").startswith(SHEET_STAMP_PREFIX)
        ]
        assert len(stamps) == 1

    def test_an_untouched_sheet_is_not_reported_as_edited(self, tmp_path: Path) -> None:
        result = build_design(
            EXAMPLES / "led-blinker" / "design.yaml", out_dir=tmp_path, report=Report()
        )
        sheet = next(p for p in result.written if p.suffix == ".kicad_sch")
        assert schematic_was_edited(parse(sheet.read_text(encoding="utf-8"))) is False

    def test_an_edited_sheet_is_detected_and_the_rebuild_says_so(
        self, tmp_path: Path
    ) -> None:
        """The policy: generated layout wins, and never silently."""
        design = EXAMPLES / "led-blinker" / "design.yaml"
        result = build_design(design, out_dir=tmp_path, report=Report())
        sheet = next(p for p in result.written if p.suffix == ".kicad_sch")

        text = sheet.read_text(encoding="utf-8")
        start = text.index('(symbol\n\t\t(lib_id "Device:C"')
        at = text.index("(at ", start)
        end = text.index(")", at)
        sheet.write_text(text[:at] + "(at 200 200 0)" + text[end + 1 :], encoding="utf-8")

        assert schematic_was_edited(parse(sheet.read_text(encoding="utf-8"))) is True

        report = Report()
        build_design(design, out_dir=tmp_path, report=report)
        codes = [w.code for w in report.warnings]
        assert "schematic-edits-discarded" in codes
        # And the edit really is gone: the sheet is the generated one again.
        assert schematic_was_edited(parse(sheet.read_text(encoding="utf-8"))) is False
        assert "(at 200 200 0)" not in sheet.read_text(encoding="utf-8")

    def test_a_sheet_with_no_stamp_answers_unknown(self, tmp_path: Path) -> None:
        """Somebody else's file is not somebody's edit, and is not reported as one."""
        netlist = compile_netlist(EXAMPLES / "led-blinker" / "design.yaml", Report())
        root = build_schematic(netlist)  # unstamped
        assert schematic_was_edited(root) is None

    def test_the_stamp_does_not_move_when_nothing_else_does(self, tmp_path: Path) -> None:
        netlist = compile_netlist(EXAMPLES / "led-blinker" / "design.yaml", Report())
        first = dump(stamp_schematic(build_schematic(netlist)))
        second = dump(stamp_schematic(build_schematic(netlist)))
        assert first == second


# ---------------------------------------------------------------------------
# M14e -- the bridge
# ---------------------------------------------------------------------------


class TestDsnProtection:
    def test_existing_copper_is_fixed_and_only_in_the_wiring_section(self) -> None:
        """`(type route)` invites the external router to rip copper up.

        Only the ``wiring`` section is rewritten: the same token appears in
        ``structure`` rules meaning something else, and a global replace would
        corrupt the file.
        """
        from aipcb.kicad.specctra import protect_existing_copper

        dsn = (
            "(pcb x\n"
            "  (structure (rule (type route)))\n"
            "  (wiring\n"
            "    (wire (path F.Cu 250 0 0 1 1)(net GND)(type route))\n"
            "    (via V 0 0 (net GND)(type route))\n"
            "  )\n"
            ")\n"
        )
        patched, count = protect_existing_copper(dsn)
        assert count == 2
        assert patched.count("(type fix)") == 2
        assert "(structure (rule (type route)))" in patched

    def test_a_file_with_no_wiring_is_left_alone(self) -> None:
        from aipcb.kicad.specctra import protect_existing_copper

        patched, count = protect_existing_copper("(pcb x (structure))\n")
        assert count == 0
        assert patched == "(pcb x (structure))\n"


class TestSplice:
    """The measured defect: SES import *replaces* a board's routing, it does not add.

    Importing a session that routed four ISP signals into ``examples/mcu-4layer``
    removed 97 tracks and 52 stitching vias. Nothing said so -- the file parsed, the
    import succeeded, and DRC found nothing wrong, because copper that is gone
    violates no rule. So only the copper for nets that were actually pending is
    lifted out of the import, and everything else on the board is kept.
    """

    def board(self, nets: dict[str, str], copper: list[tuple[str, str]]) -> object:
        from aipcb.kicad.sexpr import SNode, num, quoted, sym

        root = SNode("kicad_pcb")
        for code, name in nets.items():
            root.add(SNode("net").add(sym(code), quoted(name)))
        for code, uuid in copper:
            root.add(
                SNode("segment").add(
                    SNode("start").add(num(0), num(0)),
                    SNode("end").add(num(1), num(1)),
                    SNode("net").add(sym(code)),
                    SNode("uuid").add(quoted(uuid)),
                )
            )
        return root

    def test_only_the_wanted_nets_are_taken_and_the_rest_is_kept(self) -> None:
        original = self.board({"1": "GND", "2": "SIG_A"}, [("1", "keep-me")])
        # The importer renumbers, so the codes deliberately do not match.
        imported = self.board(
            {"7": "SIG_A", "8": "GND"}, [("7", "new-a"), ("8", "router-touched-gnd")]
        )
        merged, stats = splice_session_copper(original, imported, {"SIG_A"})

        assert stats.taken == {"SIG_A": 1}
        assert stats.ignored == {"GND": 1}
        assert stats.protected_kept == 1
        uuids = [s.get("uuid") for s in merged.children("segment")]
        assert "keep-me" in uuids, "the board's own copper was destroyed"
        assert "new-a" in uuids
        assert "router-touched-gnd" not in uuids

    def test_net_codes_are_remapped_to_the_target_board(self) -> None:
        original = self.board({"1": "GND", "2": "SIG_A"}, [])
        imported = self.board({"7": "SIG_A"}, [("7", "new-a")])
        merged, _ = splice_session_copper(original, imported, {"SIG_A"})
        spliced = next(
            s for s in merged.children("segment") if s.get("uuid") == "new-a"
        )
        assert spliced.get("net") == "2", (
            "a track kept the exporting board's net code, which on this board means "
            "a different net entirely"
        )


@needs_kicad_libraries
class TestSessionVerification:
    def test_a_width_that_disagrees_with_the_class_is_reported(self) -> None:
        from aipcb.kicad.specctra import SesResult

        netlist = compile_netlist(EXAMPLES / "mcu-4layer" / "design.yaml", Report())
        session = SesResult(
            board=Path("x"),
            nets_touched=["RESET"],
            widths_mm={"RESET": [0.25, 0.15]},
            via_sizes_mm={"RESET": [[0.6, 0.3]]},
        )
        report = Report()
        drift = verify_against_source(session, netlist, report)
        assert [d.found for d in drift] == [0.15], (
            "0.25 mm is what the isp class says, so only the 0.15 mm track is drift"
        )
        assert "session-geometry-drift" in [w.code for w in report.warnings]

    def test_a_matching_session_produces_no_findings(self) -> None:
        from aipcb.kicad.specctra import SesResult

        netlist = compile_netlist(EXAMPLES / "mcu-4layer" / "design.yaml", Report())
        session = SesResult(
            board=Path("x"),
            nets_touched=["RESET"],
            widths_mm={"RESET": [0.25]},
            via_sizes_mm={"RESET": [[0.6, 0.3]]},
        )
        report = Report()
        assert verify_against_source(session, netlist, report) == []
        assert report.ok


@needs_pcbnew
@needs_kicad_libraries
class TestBridgeRoundTrip:
    def test_export_import_keeps_what_was_there_and_adds_what_was_asked_for(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        """The whole bridge, without an external router in it.

        The session file is hand-written rather than produced by Freerouting, which
        keeps the test independent of any router's output while exercising exactly
        the path a real one goes through: pcbnew's importer, the splice, the
        verification and the state report.
        """
        from aipcb.kicad.specctra import export_dsn
        from aipcb.route.bridge import import_session
        from aipcb.route.pipeline import route_design

        out = tmp_path / "out"
        report = Report()
        done = route_design(manual_design, out, report)
        board_path = done.board_path
        netlist = done.build.netlist

        dsn = export_dsn(board_path, out / "d.dsn")
        assert dsn.protected > 0, "existing copper must be fixed in the DSN"
        assert "(type route)" not in dsn.path.read_text(encoding="utf-8")

        before = parse(board_path.read_text(encoding="utf-8"))
        segments_before = len(list(before.children("segment")))

        pads = _pad_positions(before, "SIG_A")
        assert len(pads) >= 2, "SIG_A should have two pads to join"
        session = out / "d.ses"
        session.write_text(_session_for("SIG_A", pads[0], pads[1]), encoding="utf-8")

        result = import_session(board_path, session, netlist, Report())
        after = parse(board_path.read_text(encoding="utf-8"))

        assert result.splice.taken.get("SIG_A", 0) > 0, "SIG_A got no copper"
        assert len(list(after.children("segment"))) > segments_before, (
            "the import removed copper instead of adding to it"
        )
        assert not result.still_pending
        assert {n.net: n.state for n in result.states.nets}["SIG_A"] == "manual-routed"

    def test_the_check_after_import_sees_the_imported_copper(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        """The check that follows an import must look at the board, not at a rebuild.

        `check_design` builds *fresh* on purpose since M13.5, so a check is a
        function of the source alone -- and a fresh build of any design has no copper
        on it. Checking that instead of the board just written reported DRC on an
        empty board: zero errors, and meaningless. The symptom that distinguishes the
        two is the net states, so that is what this asserts.
        """
        from aipcb.checks.loop import check_design
        from aipcb.route.bridge import import_session
        from aipcb.route.pipeline import route_design

        out = tmp_path / "out"
        done = route_design(manual_design, out, Report())
        board_path = done.board_path
        pads = _pad_positions(parse(board_path.read_text(encoding="utf-8")), "SIG_A")
        session = out / "d.ses"
        session.write_text(_session_for("SIG_A", pads[0], pads[1]), encoding="utf-8")
        import_session(board_path, session, done.build.netlist, Report())

        checked = check_design(
            manual_design,
            report=Report(),
            route=False,
            schematic=False,
            board=False,
            board_file=board_path,
        )
        assert checked.states is not None
        states = {n.net: n.state for n in checked.states.nets}
        assert states["SIG_A"] == "manual-routed", (
            "the check looked at a board without the imported copper on it"
        )
        assert not checked.states.pending

    def test_a_rebuild_preserves_imported_copper(
        self, manual_design: Path, tmp_path: Path
    ) -> None:
        """Imported copper is manual copper: M6 keeps it, and the router goes round."""
        from aipcb.route.bridge import import_session
        from aipcb.route.pipeline import route_design

        out = tmp_path / "out"
        done = route_design(manual_design, out, Report())
        board_path = done.board_path
        pads = _pad_positions(
            parse(board_path.read_text(encoding="utf-8")), "SIG_A"
        )
        session = out / "d.ses"
        session.write_text(_session_for("SIG_A", pads[0], pads[1]), encoding="utf-8")
        import_session(board_path, session, done.build.netlist, Report())

        build_design(manual_design, out_dir=out, report=Report())
        rebuilt = parse(board_path.read_text(encoding="utf-8"))
        assert "SIG_A" in nets_with_copper(rebuilt, done.build.netlist)


def _pad_positions(board: object, net_name: str) -> list[tuple[float, float]]:
    """Where a net's pads sit on the board, in millimetres."""
    import math

    code = next(
        (n.value(0) for n in board.children("net") if n.value(1) == net_name), None
    )
    found: list[tuple[float, float]] = []
    for footprint in board.children("footprint"):
        at = footprint.child("at")
        if at is None:
            continue
        atoms = at.atoms()
        ox, oy = float(atoms[0].value), float(atoms[1].value)
        angle = math.radians(float(atoms[2].value)) if len(atoms) > 2 else 0.0
        for pad in footprint.children("pad"):
            net = pad.child("net")
            if net is None or net.value(0) != code:
                continue
            pad_at = pad.child("at")
            if pad_at is None:
                continue
            coords = pad_at.atoms()
            px, py = float(coords[0].value), float(coords[1].value)
            cos, sin = math.cos(angle), math.sin(angle)
            found.append((ox + px * cos + py * sin, oy - px * sin + py * cos))
    return found


def _session_for(
    net: str, start: tuple[float, float], end: tuple[float, float]
) -> str:
    """A minimal Specctra session joining two points, in the units KiCad exports."""

    def um(value: float) -> str:
        return f"{value * 1000:.0f}"

    return f"""(session d.ses
  (base_design d.dsn)
  (placement
    (resolution um 10)
  )
  (was_is
  )
  (routes
    (resolution um 10)
    (parser
      (host_cad "test")
      (host_version "1")
    )
    (library_out
    )
    (network_out
      (net {net}
        (wire
          (path F.Cu 250
            {um(start[0])} {um(-start[1])}
            {um(end[0])} {um(-end[1])}
          )
        )
      )
    )
  )
)
"""

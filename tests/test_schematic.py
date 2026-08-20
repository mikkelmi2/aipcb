"""Compiling designs into KiCad schematics.

The acceptance bar for M2 is not "it wrote a file" but "KiCad agrees with it": the
schematic must pass ERC, and the netlist KiCad extracts from it must be exactly the
netlist the source describes. Both are checked here against the real ``kicad-cli``,
and both skip with a clear reason when KiCad is absent.
"""

from __future__ import annotations

import collections
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aipcb.compile.build import build_design, compile_netlist
from aipcb.compile.geometry import Point, place_point
from aipcb.compile.schematic import undriven_power_nets
from aipcb.diagnostics import AipcbError, Report
from aipcb.kicad.cli import run_kicad
from aipcb.kicad.sexpr import parse
from aipcb.kicad.symbols import resolve_symbol

from .conftest import REPO_ROOT, needs_kicad_cli, needs_kicad_libraries

GOLDEN = REPO_ROOT / "tests" / "golden"


def build(design: Path, tmp_path: Path) -> tuple[Path, Report]:
    report = Report()
    result = build_design(design, out_dir=tmp_path, report=report)
    schematic = next(p for p in result.written if p.suffix == ".kicad_sch")
    return schematic, report


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestPinPlacement:
    """The symbol-space to sheet-space transform.

    These values are not invented: they are read out of KiCad's own
    ``pic_programmer`` demo, where ``R1`` sits at (78.74, 43.18) rotated 90 degrees
    and its two pins land on wire endpoints at x = 82.55 and x = 74.93.
    """

    def test_unrotated_symbol_mirrors_y(self) -> None:
        assert place_point(Point(100, 100), 0, 0.0, 3.81).rounded() == Point(100.0, 96.19)

    def test_rotation_matches_kicad(self) -> None:
        assert place_point(Point(78.74, 43.18), 90, 0.0, 3.81).rounded() == Point(82.55, 43.18)
        assert place_point(Point(78.74, 43.18), 90, 0.0, -3.81).rounded() == Point(74.93, 43.18)

    def test_pin_outward_angle_is_away_from_the_body(self) -> None:
        resistor = resolve_symbol("Device:R")
        pin = resistor.pin("1")
        assert pin is not None
        # Pin 1 sits above the body at y=+3.81 with its angle pointing down into it.
        assert pin.angle == 270.0
        assert pin.outward_angle == 90.0


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestSchematicStructure:
    def test_output_parses_as_kicad_sexpr(self, example_design: Path, tmp_path: Path) -> None:
        schematic, _ = build(example_design, tmp_path)
        root = parse(schematic.read_text(encoding="utf-8"))
        assert root.name == "kicad_sch"
        assert root.get("version") == "20250114"

    def test_every_component_appears_once(self, example_design: Path, tmp_path: Path) -> None:
        netlist = compile_netlist(example_design, Report())
        root = parse(build(example_design, tmp_path)[0].read_text(encoding="utf-8"))
        refs = collections.Counter(
            path.get("reference")
            for symbol in root.children("symbol")
            if (inst := symbol.child("instances")) is not None
            for project in inst.children("project")
            for path in project.children("path")
        )
        for refdes in netlist.components:
            assert refs[refdes] == 1, f"{refdes} appears {refs[refdes]} times"

    def test_symbols_are_embedded_and_self_contained(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """A derived symbol must be merged with its base, or KiCad cannot draw it."""
        root = parse(build(example_design, tmp_path)[0].read_text(encoding="utf-8"))
        lib_symbols = root.child("lib_symbols")
        assert lib_symbols is not None
        embedded = list(lib_symbols.children("symbol"))
        assert embedded
        for symbol in embedded:
            assert symbol.child("extends") is None, f"{symbol.value(0)} still extends"
            pins = sum(len(list(u.children("pin"))) for u in symbol.children("symbol"))
            assert pins > 0, f"{symbol.value(0)} has no pins"

    def test_uuids_come_from_source_paths(self, example_design: Path, tmp_path: Path) -> None:
        netlist = compile_netlist(example_design, Report())
        root = parse(build(example_design, tmp_path)[0].read_text(encoding="utf-8"))
        emitted = {
            s.get("uuid") for s in root.children("symbol") if s.get("uuid") is not None
        }
        for component in netlist.components.values():
            assert component.uuid in emitted

    def test_no_date_is_written(self, example_design: Path, tmp_path: Path) -> None:
        """A timestamp would make every rebuild a diff."""
        root = parse(build(example_design, tmp_path)[0].read_text(encoding="utf-8"))
        title = root.child("title_block")
        assert title is not None
        assert title.child("date") is None

    def test_unconnected_pins_get_no_connect_markers(self, tmp_path: Path) -> None:
        design = REPO_ROOT / "examples" / "led-blinker" / "design.yaml"
        root = parse(build(design, tmp_path)[0].read_text(encoding="utf-8"))
        # The ATtiny's PB3 is deliberately unused.
        assert len(list(root.children("no_connect"))) == 1


@needs_kicad_libraries
class TestPowerFlags:
    def test_flags_only_undriven_rails(self) -> None:
        """A PWR_FLAG on an already-driven rail is itself an ERC error."""
        from aipcb.compile.schematic import _resolve_symbols

        netlist = compile_netlist(
            REPO_ROOT / "examples" / "ldo-supply" / "design.yaml", Report()
        )
        symbols = _resolve_symbols(netlist, needs_power_flag=False)
        flagged = undriven_power_nets(netlist, symbols)
        # VIN and GND come in on a passive header; VOUT is driven by the regulator.
        assert "VOUT" not in flagged
        assert "VIN" in flagged

    def test_flag_reference_designators_are_numbered(self, tmp_path: Path) -> None:
        """An unnumbered refdes would make KiCad re-annotate the file on load."""
        design = REPO_ROOT / "examples" / "ldo-supply" / "design.yaml"
        root = parse(build(design, tmp_path)[0].read_text(encoding="utf-8"))
        flags = [
            s for s in root.children("symbol") if s.get("lib_id") == "power:PWR_FLAG"
        ]
        assert flags
        for flag in flags:
            reference = next(
                p.value(1) for p in flag.children("property") if p.value(0) == "Reference"
            )
            assert reference is not None
            assert reference.startswith("#FLG")
            assert reference[4:].isdigit()


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


@needs_kicad_libraries
class TestDeterminism:
    def test_two_builds_are_byte_identical(self, example_design: Path, tmp_path: Path) -> None:
        first = build(example_design, tmp_path / "a")[0].read_bytes()
        second = build(example_design, tmp_path / "b")[0].read_bytes()
        assert first == second

    def test_matches_the_golden_file(self, example_design: Path, tmp_path: Path) -> None:
        golden = GOLDEN / example_design.parent.name
        if not golden.is_dir():
            pytest.skip(f"no golden files for {example_design.parent.name}")
        build_design(example_design, out_dir=tmp_path)
        for expected in sorted(golden.iterdir()):
            actual = tmp_path / expected.name
            assert actual.exists(), f"{expected.name} was not produced"
            assert actual.read_text(encoding="utf-8") == expected.read_text(
                encoding="utf-8"
            ), (
                f"{expected.name} differs from the golden file. If the change is "
                "intended, run `python -m tests.regenerate_golden` and review the diff."
            )

    def test_unchanged_output_is_not_rewritten(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        schematic, _ = build(example_design, tmp_path)
        before = schematic.stat().st_mtime_ns
        build(example_design, tmp_path)
        assert schematic.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# refusal
# ---------------------------------------------------------------------------


class TestBuildRefusal:
    def test_does_not_write_when_validation_fails(self, write_design, tmp_path: Path) -> None:
        """Quietly wrong output is worse than none."""
        design = write_design(
            "name: broken\ncomponents:\n  U1:\n    part: NoSuchPart\n"
            '    pins: { "1": A, "2": B }\n'
        )
        out = tmp_path / "out"
        with pytest.raises(AipcbError) as excinfo:
            build_design(design, out_dir=out)
        assert any(d.code == "unknown-part" for d in excinfo.value.report)
        assert not list(out.glob("*.kicad_sch"))


# ---------------------------------------------------------------------------
# what KiCad thinks
# ---------------------------------------------------------------------------


@needs_kicad_libraries
@needs_kicad_cli
class TestAgainstKicad:
    def test_erc_passes(self, example_design: Path, tmp_path: Path) -> None:
        schematic, _ = build(example_design, tmp_path)
        report_path = tmp_path / "erc.json"
        run = run_kicad(
            "sch", "erc", "--format", "json", "--severity-all",
            "-o", str(report_path), str(schematic),
        )
        assert run.returncode == 0, run.stderr
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        violations = [
            f"[{v['severity']}] {v['type']}: {v['description']}"
            for sheet in payload["sheets"]
            for v in sheet["violations"]
        ]
        assert not violations, "\n".join(violations)

    def test_kicad_extracts_the_netlist_we_meant(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        """The real correctness test: does KiCad connect what the source connects?"""
        schematic, _ = build(example_design, tmp_path)
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
                # Power flags are scaffolding for ERC, not part of the design.
                if not str(node.get("ref")).startswith("#")
            }
            # A pin marked no-connect becomes its own one-pin net in KiCad's output.
            if len(members) > 1:
                from_kicad[str(net.get("name"))] = members

        from_source = {
            net.name: {f"{node.refdes}.{node.pin}" for node in net.nodes}
            for net in netlist.sorted_nets()
        }

        actual = sorted(tuple(sorted(m)) for m in from_kicad.values())
        expected = sorted(tuple(sorted(m)) for m in from_source.values())
        assert actual == expected

    def test_schematic_renders(self, example_design: Path, tmp_path: Path) -> None:
        """Rendering exercises the embedded symbol graphics, which ERC does not."""
        schematic, _ = build(example_design, tmp_path)
        out = tmp_path / "plot.pdf"
        run = run_kicad("sch", "export", "pdf", "-o", str(out), str(schematic))
        assert run.returncode == 0, run.stderr
        assert out.exists() and out.stat().st_size > 1000

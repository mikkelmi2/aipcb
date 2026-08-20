"""The bundled examples must validate cleanly and survive a round trip.

These are the designs the README points at, so they are also the format's
documentation. If one of them stops validating, the documentation is wrong.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from aipcb.checks.kicad_bindings import check_kicad_bindings
from aipcb.checks.semantic import run_semantic_checks
from aipcb.diagnostics import Report, Severity
from aipcb.elaborate import elaborate
from aipcb.loader import load_design
from aipcb.model.design import Design

from .conftest import needs_kicad_libraries


def full_report(design_path: Path) -> Report:
    report = Report()
    loaded = load_design(design_path, report=report)
    netlist = elaborate(loaded, report)
    run_semantic_checks(netlist, report)
    check_kicad_bindings(loaded, report)
    return report


def test_example_validates_without_errors(example_design: Path) -> None:
    report = full_report(example_design)
    assert report.ok, report.render()


def test_example_has_no_warnings(example_design: Path) -> None:
    """Warnings in an example mean either a bad example or a bad check."""
    report = full_report(example_design)
    warnings = [d for d in report if d.severity is Severity.WARNING]
    assert not warnings, "\n".join(d.render() for d in warnings)


@needs_kicad_libraries
def test_example_binds_to_real_kicad_libraries(example_design: Path) -> None:
    report = Report()
    loaded = load_design(example_design, report=report)
    check_kicad_bindings(loaded, report)
    problems = [d for d in report if d.code != "kicad-libraries-missing"]
    assert not problems, "\n".join(d.render() for d in problems)


class TestRoundTrip:
    """Dumping a validated design and reloading it must produce the same design."""

    def test_model_survives_a_yaml_round_trip(
        self, example_design: Path, tmp_path: Path
    ) -> None:
        original = load_design(example_design).design
        dumped = yaml.safe_dump(
            original.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
        )
        reparsed = Design.model_validate(yaml.safe_load(dumped))
        assert reparsed == original

    def test_dump_is_stable(self, example_design: Path) -> None:
        design = load_design(example_design).design
        once = design.model_dump(mode="json", by_alias=True)
        twice = Design.model_validate(once).model_dump(mode="json", by_alias=True)
        assert once == twice

    def test_elaboration_is_deterministic(self, example_design: Path) -> None:
        first = elaborate(load_design(example_design))
        second = elaborate(load_design(example_design))
        assert {c.refdes: c.uuid for c in first.components.values()} == {
            c.refdes: c.uuid for c in second.components.values()
        }
        assert {n: net.uuid for n, net in first.nets.items()} == {
            n: net.uuid for n, net in second.nets.items()
        }


class TestCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validate_exits_zero(self, example_design: Path) -> None:
        result = self._run("validate", str(example_design))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_validate_json_is_parseable(self, example_design: Path) -> None:
        result = self._run("validate", str(example_design), "--json")
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["design"]["components"] > 0

    def test_missing_file_exits_two(self, tmp_path: Path) -> None:
        result = self._run("validate", str(tmp_path / "nope.yaml"))
        assert result.returncode == 2
        assert "no such file" in result.stderr

    def test_schema_command_emits_json_schema(self) -> None:
        result = self._run("schema")
        schema = json.loads(result.stdout)
        assert "properties" in schema
        assert "components" in schema["properties"]

    def test_parts_lists_the_library(self, example_design: Path) -> None:
        result = self._run("parts", str(example_design), "--json")
        parts = json.loads(result.stdout)
        assert parts, "expected the example to load some parts"
        assert all("symbol" in p for p in parts.values())

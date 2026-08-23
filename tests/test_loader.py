# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Schema validation, and the quality of the messages it produces."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aipcb.diagnostics import AipcbError, Report
from aipcb.loader import load_design

MINIMAL = """
name: t
components:
  U1:
    part: P
    pins: { "1": A, "2": B }
"""


def diagnostics_for(write_design: Callable[[str], Path], text: str) -> Report:
    with pytest.raises(AipcbError) as excinfo:
        load_design(write_design(text))
    return excinfo.value.report


class TestSchemaDiagnostics:
    def test_unknown_field_suggests_the_right_one(self, write_design) -> None:
        report = diagnostics_for(
            write_design,
            "name: t\ncomponents:\n  U1:\n    part: P\n    pin: {}\n",
        )
        diag = report.diagnostics[0]
        assert diag.code == "schema-extra-forbidden"
        assert "unknown field 'pin'" in diag.message
        assert diag.hint == "did you mean 'pins'?"

    def test_unknown_field_lists_alternatives_when_nothing_is_close(
        self, write_design
    ) -> None:
        report = diagnostics_for(
            write_design,
            "name: t\ncomponents:\n  U1:\n    part: P\n    zzzzzz: 1\n",
        )
        assert "allowed fields here" in (report.diagnostics[0].hint or "")

    def test_missing_field(self, write_design) -> None:
        report = diagnostics_for(write_design, "name: t\ncomponents:\n  U1:\n    role: mcu\n")
        codes = {d.code for d in report}
        assert "schema-missing" in codes
        assert any("'part'" in d.message for d in report)

    def test_diagnostics_carry_a_position(self, write_design) -> None:
        report = diagnostics_for(
            write_design,
            "name: t\ncomponents:\n  U1:\n    part: P\n    pin: {}\n",
        )
        loc = report.diagnostics[0].loc
        assert loc is not None and loc.line == 5

    def test_our_own_validator_messages_survive(self, write_design) -> None:
        report = diagnostics_for(
            write_design,
            'name: t\ncomponents:\n  U1:\n    part: P\n    refdes: "bad"\n',
        )
        assert any("letters followed by digits" in d.message for d in report)
        assert not any(d.message.startswith("Value error") for d in report)

    def test_empty_design_is_rejected(self, write_design) -> None:
        report = diagnostics_for(write_design, "name: t\n")
        assert any("at least one component" in d.message for d in report)


class TestPartLibraries:
    def test_missing_library_is_reported_not_raised(self, write_design) -> None:
        report = Report()
        load_design(
            write_design("name: t\nlibraries: [absent.yaml]\ncomponents:\n  U1:\n"
                         '    part: P\n    pins: { "1": A }\n'),
            report=report,
        )
        assert any(d.code == "library-unreadable" for d in report)

    def test_duplicate_part_across_libraries_is_an_error(
        self, write_design, tmp_path: Path
    ) -> None:
        body = (
            "parts:\n  P:\n    symbol: Device:R\n"
            "    footprint: Resistor_SMD:R_0603_1608Metric\n"
            '    pins: { "1": {}, "2": {} }\n'
        )
        (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
        report = Report()
        load_design(
            write_design(
                "name: t\nlibraries: [a.yaml, b.yaml]\ncomponents:\n  U1:\n"
                '    part: P\n    pins: { "1": A, "2": B }\n'
            ),
            report=report,
        )
        duplicates = [d for d in report if d.code == "duplicate-part"]
        assert len(duplicates) == 1
        assert "already defined at" in (duplicates[0].hint or "")

    def test_parts_load(self, write_design, tmp_path: Path) -> None:
        (tmp_path / "lib.yaml").write_text(
            "parts:\n  P:\n    symbol: Device:R\n"
            "    footprint: Resistor_SMD:R_0603_1608Metric\n"
            '    pins: { "1": {}, "2": {} }\n',
            encoding="utf-8",
        )
        loaded = load_design(
            write_design(
                "name: t\nlibraries: [lib.yaml]\ncomponents:\n  U1:\n"
                '    part: P\n    pins: { "1": A, "2": B }\n'
            )
        )
        assert set(loaded.parts) == {"P"}
        assert loaded.parts["P"].symbol == "Device:R"


class TestReportOutput:
    def test_json_is_machine_readable(self, write_design) -> None:
        import json

        report = diagnostics_for(
            write_design, "name: t\ncomponents:\n  U1:\n    part: P\n    pin: {}\n"
        )
        payload = json.loads(report.to_json())
        assert payload["ok"] is False
        assert payload["counts"]["error"] == 1
        entry = payload["diagnostics"][0]
        assert entry["path_text"] == "components.U1.pin"
        assert entry["location"]["line"] == 5

    def test_errors_sort_before_warnings(self) -> None:
        report = Report()
        report.warning("w", "a warning")
        report.error("e", "an error")
        assert [d.code for d in report.sorted()] == ["e", "w"]

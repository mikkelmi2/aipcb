# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Semantic checks: each one should fire when it should, and stay quiet otherwise."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aipcb.checks.semantic import run_semantic_checks
from aipcb.diagnostics import Report
from aipcb.elaborate import elaborate
from aipcb.loader import load_design

LIB = """
parts:
  R:
    symbol: Device:R
    footprint: Resistor_SMD:R_0603_1608Metric
    pins: { "1": {}, "2": {} }
  C_LOW_V:
    symbol: Device:C
    footprint: Capacitor_SMD:C_0603_1608Metric
    pins: { "1": {}, "2": {} }
    limits: { voltage_max_v: 6.3 }
  REG:
    symbol: Regulator_Linear:AMS1117-3.3
    footprint: Package_TO_SOT_SMD:SOT-223-3_TabPin2
    pins:
      "1": { type: power_in, name: GND }
      "2": { type: power_out, name: VO }
      "3": { type: power_in, name: VI }
"""


@pytest.fixture
def check(write_design: Callable[..., Path], tmp_path: Path):
    (tmp_path / "lib.yaml").write_text(LIB, encoding="utf-8")

    def _check(text: str) -> Report:
        report = Report()
        loaded = load_design(write_design(text), report=report)
        run_semantic_checks(elaborate(loaded, report), report)
        return report

    return _check


def codes(report: Report) -> set[str]:
    return {d.code for d in report}


class TestDanglingNets:
    def test_single_node_net_is_an_error(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": C } }\n'
        )
        dangling = [d for d in report if d.code == "dangling-net"]
        assert {d.context["net"] for d in dangling} == {"B", "C"}

    def test_declared_but_unused_net(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n  ORPHAN: {}\ncomponents:\n"
            '  R1: { part: R, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
        )
        diag = next(d for d in report if d.context.get("net") == "ORPHAN")
        assert "nothing connects to it" in (diag.hint or "")

    def test_fully_connected_design_is_quiet(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
        )
        assert "dangling-net" not in codes(report)


class TestIntentChecks:
    def test_for_must_name_a_real_component(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, for: U9, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
        )
        diag = next(d for d in report if d.code == "unknown-reference")
        assert "U9" in diag.message
        assert "same scope" in (diag.hint or "")

    def test_decoupling_without_a_target_warns(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, role: decoupling, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
        )
        assert "role-without-target" in codes(report)

    def test_unknown_role_warns_but_does_not_fail(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, role: flibbertigibbet, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
        )
        assert "unknown-role" in codes(report)
        assert report.ok

    def test_constraint_members_must_exist(self, check) -> None:
        report = check(
            'name: t\nlibraries: [lib.yaml]\ncomponents:\n'
            '  R1: { part: R, pins: { "1": A, "2": B } }\n'
            '  R2: { part: R, pins: { "1": A, "2": B } }\n'
            "constraints:\n"
            "  - kind: max_distance\n    between: [R1, GHOST]\n    mm: 5\n    reason: x\n"
        )
        assert "unknown-constraint-member" in codes(report)


class TestDiffPairs:
    BASE = (
        'name: t\nlibraries: [lib.yaml]\nnets:\n{nets}\ncomponents:\n'
        '  R1: {{ part: R, pins: {{ "1": DP, "2": DM }} }}\n'
        '  R2: {{ part: R, pins: {{ "1": DP, "2": DM }} }}\n'
    )

    def test_symmetric_pair_is_accepted(self, check) -> None:
        report = check(
            self.BASE.format(
                nets="  DP: { class: diff_pair, diff_pair: DM }\n"
                "  DM: { class: diff_pair, diff_pair: DP }\n"
            )
        )
        assert report.ok, report.render()

    def test_one_sided_declaration_is_an_error(self, check) -> None:
        report = check(
            self.BASE.format(
                nets="  DP: { class: diff_pair, diff_pair: DM }\n"
                "  DM: { class: diff_pair }\n"
            )
        )
        diag = next(d for d in report if d.code == "asymmetric-diff-pair")
        assert "set `diff_pair: DP`" in (diag.hint or "")

    def test_partner_must_exist(self, check) -> None:
        report = check(
            self.BASE.format(
                nets="  DP: { class: diff_pair, diff_pair: GHOST }\n  DM: {}\n"
            )
        )
        assert "unknown-diff-pair" in codes(report)

    def test_mismatched_classes_warn(self, check) -> None:
        report = check(
            self.BASE.format(
                nets="  DP: { class: diff_pair, diff_pair: DM }\n"
                "  DM: { class: signal, diff_pair: DP }\n"
            )
        )
        assert "diff-pair-class-mismatch" in codes(report)


class TestElectricalChecks:
    def test_voltage_rating_exceeded(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n"
            "  HV: { class: power, voltage: 12.0 }\n  GND: { class: ground }\n"
            "components:\n"
            '  C1: { part: C_LOW_V, pins: { "1": HV, "2": GND } }\n'
            '  R1: { part: R, pins: { "1": HV, "2": GND } }\n'
        )
        diag = next(d for d in report if d.code == "voltage-rating-exceeded")
        assert "6.3 V" in diag.message and "12.0 V" in diag.message

    def test_thin_margin_warns(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n"
            "  V: { class: power, voltage: 5.5 }\n  GND: { class: ground }\n"
            "components:\n"
            '  C1: { part: C_LOW_V, pins: { "1": V, "2": GND } }\n'
            '  R1: { part: R, pins: { "1": V, "2": GND } }\n'
        )
        assert "voltage-derating" in codes(report)
        assert "voltage-rating-exceeded" not in codes(report)

    def test_comfortable_margin_is_quiet(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n"
            "  V: { class: power, voltage: 3.3 }\n  GND: { class: ground }\n"
            "components:\n"
            '  C1: { part: C_LOW_V, pins: { "1": V, "2": GND } }\n'
            '  R1: { part: R, pins: { "1": V, "2": GND } }\n'
        )
        assert "voltage-derating" not in codes(report)

    def test_undriven_power_net_warns(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n  V: { class: power }\n"
            "components:\n"
            '  R1: { part: R, pins: { "1": V, "2": GND } }\n'
            '  R2: { part: R, pins: { "1": V, "2": GND } }\n'
        )
        assert "undriven-power-net" in codes(report)

    def test_a_regulator_output_counts_as_a_driver(self, check) -> None:
        report = check(
            "name: t\nlibraries: [lib.yaml]\nnets:\n"
            "  VOUT: { class: power }\n  VIN: { class: power }\n"
            "  GND: { class: ground }\n"
            "components:\n"
            '  U1: { part: REG, pins: { VI: VIN, VO: VOUT, GND: GND } }\n'
            '  R1: { part: R, pins: { "1": VOUT, "2": GND } }\n'
            '  R2: { part: R, pins: { "1": VIN, "2": GND } }\n'
        )
        undriven = {d.context.get("net") for d in report if d.code == "undriven-power-net"}
        assert "VOUT" not in undriven

# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The benchmark harness: it measures, and it notices when a measurement moves.

`aipcb bench` is not a test and these are not assertions about wall clock -- a
threshold on a timing is flaky by construction, which is why the harness reports
and `--compare` decides. What is testable is everything around the numbers: that
the metrics are computed from the board rather than invented, that a results file
survives a round trip, and above all that `--compare` actually catches a
regression when one is put in front of it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from aipcb import bench as harness
from aipcb.diagnostics import Report

from .conftest import REPO_ROOT, needs_kicad_libraries


def _board(**overrides: object) -> harness.BoardBench:
    """A benchmark record with plausible numbers, for the comparison tests."""
    base = harness.BoardBench(
        example="demo",
        nets=8,
        components=6,
        attempted=13,
        routed=13,
        failed=0,
        completion=1.0,
        length_mm=262.722,
        segments=46,
        vias=1,
        layer_changes=1,
        via_lower_bound=0,
        via_excess=1,
        layer_changes_without_pressure=1,
        iterations=1,
        converged=True,
        crossings=0,
        self_crossings=0,
        over_subscribed=0,
        seconds={"tighten": 0.25, "negotiate": 0.03},
        total_seconds=1.2,
        router_seconds=0.28,
        layers=[
            harness.LayerBench(
                layer="F.Cu",
                cuts=379,
                special_cuts=287,
                charged=90,
                utilization_p50=0.2,
                utilization_p95=0.6,
                utilization_max=0.9,
                over_subscribed=0,
                headroom_mm=0.4,
            )
        ],
        board_sha256="a" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _result(*boards: harness.BoardBench) -> harness.BenchResult:
    return harness.BenchResult(
        commit="0000000",
        recorded="2026-08-23T00:00:00+00:00",
        environment={"python": "3.12.0"},
        boards=list(boards),
    )


class TestComparison:
    """The half of the harness that has an opinion."""

    def test_an_identical_run_is_clean(self) -> None:
        outcome = harness.compare(_result(_board()), _result(_board()))
        assert outcome.ok
        assert not outcome.regressions
        assert not outcome.changes

    def test_a_dropped_connection_is_a_regression(self) -> None:
        degraded = _board(routed=11, failed=2, completion=11 / 13)
        outcome = harness.compare(_result(_board()), _result(degraded))
        assert not outcome.ok
        assert any("completion" in entry for entry in outcome.regressions)

    def test_more_copper_beyond_the_threshold_is_a_regression(self) -> None:
        outcome = harness.compare(
            _result(_board()), _result(_board(length_mm=280.0))
        )
        assert not outcome.ok
        assert any("copper" in entry for entry in outcome.regressions)

    def test_a_little_more_copper_is_not(self) -> None:
        """1% is under the 2% default, so it passes -- and is not silently hidden."""
        outcome = harness.compare(
            _result(_board()), _result(_board(length_mm=265.3))
        )
        assert outcome.ok

    def test_less_copper_is_an_improvement_not_a_regression(self) -> None:
        outcome = harness.compare(
            _result(_board()), _result(_board(length_mm=230.0))
        )
        assert outcome.ok
        assert any("copper" in entry for entry in outcome.improvements)

    def test_a_slower_run_beyond_the_threshold_is_a_regression(self) -> None:
        outcome = harness.compare(
            _result(_board()), _result(_board(router_seconds=0.9))
        )
        assert not outcome.ok
        assert any("routing" in entry for entry in outcome.regressions)

    def test_a_generous_threshold_forgives_it(self) -> None:
        """What the CI smoke run does, because it compares across machines."""
        outcome = harness.compare(
            _result(_board()),
            _result(_board(router_seconds=0.9)),
            runtime_threshold=500.0,
        )
        assert outcome.ok

    def test_a_new_crossing_is_a_regression(self) -> None:
        outcome = harness.compare(_result(_board()), _result(_board(crossings=1)))
        assert not outcome.ok
        assert any("crossings" in entry for entry in outcome.regressions)

    def test_a_changed_board_is_reported_without_being_a_failure(self) -> None:
        """A different hash is news. It is not, on its own, bad news."""
        outcome = harness.compare(
            _result(_board()), _result(_board(board_sha256="b" * 64))
        )
        assert outcome.ok
        assert any("the board changed" in entry for entry in outcome.changes)

    def test_a_board_missing_from_the_run_is_named(self) -> None:
        outcome = harness.compare(_result(_board()), _result())
        assert outcome.missing == ["demo: not in this run"]

    def test_a_subset_run_is_not_missing_what_it_never_asked_for(self) -> None:
        """What the CI smoke run does: three boards against an eleven-board file."""
        outcome = harness.compare(_result(_board()), _result(), expect_all=False)
        assert outcome.missing == []
        assert outcome.ok


class TestResultsFile:
    def test_a_results_file_survives_a_round_trip(self, tmp_path: Path) -> None:
        result = _result(_board(), _board(example="other"))
        path = tmp_path / "results.json"
        harness.write(result, path)
        again = harness.load(path)
        assert again.to_dict() == result.to_dict()

    def test_a_file_from_another_schema_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """A baseline outlives the code that wrote it, so the failure must be legible."""
        path = tmp_path / "results.json"
        harness.write(_result(_board()), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = 99
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            harness.load(path)
        except ValueError as exc:
            assert "schema 99" in str(exc) and "re-measure" in str(exc)
        else:  # pragma: no cover - the call above raises
            raise AssertionError("a future schema should not load")

    def test_a_file_missing_a_metric_says_which(self, tmp_path: Path) -> None:
        path = tmp_path / "results.json"
        harness.write(_result(_board()), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["boards"][0]["layer_changes"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            harness.load(path)
        except ValueError as exc:
            assert "layer_changes" in str(exc)
        else:  # pragma: no cover - the call above raises
            raise AssertionError("a file missing a metric should not load")

    def test_the_file_records_what_it_was_measured_on(self, tmp_path: Path) -> None:
        """Wall clock without a machine attached is not a measurement."""
        path = tmp_path / "results.json"
        harness.write(_result(_board()), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["commit"] == "0000000"
        assert payload["environment"]["python"] == "3.12.0"
        assert payload["totals"]["routed"] == 13


class TestRendering:
    def test_the_table_has_a_row_per_board_and_a_total(self) -> None:
        text = harness.render_table(_result(_board(), _board(example="other")))
        assert "demo" in text and "other" in text
        assert "2 boards" in text

    def test_the_stage_table_names_every_stage_any_board_used(self) -> None:
        text = harness.render_stages(
            _result(_board(), _board(example="other", seconds={"field": 0.5}))
        )
        assert "tighten" in text and "field" in text

    def test_a_clean_comparison_says_so(self) -> None:
        assert "no differences" in harness.render_comparison(harness.Comparison())


class TestSelection:
    def test_no_names_means_every_bundled_example(self) -> None:
        designs = harness.resolve(None, REPO_ROOT)
        assert len(designs) >= 10
        assert all(d.name == "design.yaml" for d in designs)

    def test_the_smoke_subset_exists(self) -> None:
        designs = harness.resolve(harness.SMOKE_EXAMPLES, REPO_ROOT)
        assert [d.parent.name for d in designs] == list(harness.SMOKE_EXAMPLES)

    def test_an_unknown_name_says_what_is_available(self) -> None:
        try:
            harness.resolve(["nonesuch"], REPO_ROOT)
        except KeyError as exc:
            assert "led-blinker" in exc.args[0]
        else:  # pragma: no cover - the call above raises
            raise AssertionError("an unknown example should not resolve")


@needs_kicad_libraries
class TestOnARealBoard:
    """The metrics have to come off the board, not out of the harness."""

    def test_a_benchmarked_board_reports_what_it_routed(self) -> None:
        design = REPO_ROOT / "examples" / "led-blinker" / "design.yaml"
        board = harness.bench_board(design, report=Report())
        assert board.example == "led-blinker"
        assert board.routed == board.attempted > 0
        assert board.completion == 1.0
        assert board.length_mm > 0
        assert board.crossings == 0 and board.self_crossings == 0
        assert len(board.board_sha256) == 64

    def test_every_router_stage_is_timed(self) -> None:
        design = REPO_ROOT / "examples" / "congestion" / "design.yaml"
        board = harness.bench_board(design, report=Report())
        assert {"field", "negotiate", "tighten"} <= set(board.seconds)
        assert board.router_seconds > 0
        assert board.total_seconds >= board.router_seconds

    def test_the_corridors_are_charged_including_the_second_diagonals(self) -> None:
        design = REPO_ROOT / "examples" / "congestion" / "design.yaml"
        board = harness.bench_board(design, report=Report())
        assert board.layers, "a routed board has corridors"
        for layer in board.layers:
            assert layer.special_cuts > 0, layer.layer
            assert 0 < layer.charged <= layer.cuts + layer.special_cuts

    def test_the_via_bound_is_never_above_what_was_spent(self) -> None:
        """It is a lower bound on layer changes. If it exceeds them, it is not one.

        Against `layer_changes` and not `vias`, and that distinction is the reason
        the two are separate fields: two connections of a net can land on one hole,
        so the board can carry fewer holes than the router made decisions, and only
        the decisions are comparable with a bound derived per connection.
        """
        design = REPO_ROOT / "examples" / "pcie-sata" / "design.yaml"
        board = harness.bench_board(design, report=Report())
        assert board.via_lower_bound > 0, "this board has SMD pads on both sides"
        assert board.via_lower_bound <= board.layer_changes
        assert board.via_excess == board.layer_changes - board.via_lower_bound
        assert board.vias <= board.layer_changes


@needs_kicad_libraries
class TestBenchCli:
    """End to end, including the exit code a CI job reads."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aipcb.cli", "bench", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

    def test_the_smoke_run_prints_a_table_and_exits_clean(
        self, tmp_path: Path
    ) -> None:
        run = self._run("--smoke", "--out", str(tmp_path / "run.json"))
        assert run.returncode == 0, run.stderr
        assert "congestion" in run.stdout
        assert (tmp_path / "run.json").is_file()

    def test_compare_catches_a_synthetic_regression(self, tmp_path: Path) -> None:
        """The acceptance test for `--compare`: degrade a baseline, expect a flag.

        The baseline is a real measured run of one example, doctored so that this
        run looks worse than it: less copper, a faster router, one more connection
        routed. Every one of those is a regression when the current run fails to
        match it, and the command has to exit 1 so a CI job fails.
        """
        first = tmp_path / "baseline.json"
        run = self._run("--examples", "congestion", "--out", str(first))
        assert run.returncode == 0, run.stderr

        payload = json.loads(first.read_text(encoding="utf-8"))
        board = payload["boards"][0]
        board["length_mm"] = round(board["length_mm"] * 0.5, 3)
        board["router_seconds"] = round(board["router_seconds"] * 0.1, 4)
        board["vias"] = max(0, board["vias"] - 1)
        first.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        again = self._run(
            "--examples", "congestion",
            "--out", str(tmp_path / "second.json"),
            "--compare", str(first),
        )
        assert again.returncode == 1, again.stdout
        assert "regression: congestion: copper" in again.stdout
        assert "regression: congestion: routing" in again.stdout
        assert "regression: congestion: vias" in again.stdout

    def test_compare_catches_a_dropped_connection(self, tmp_path: Path) -> None:
        """The regression that matters most, on the board that can demonstrate it.

        `overconstrained` is four wires that have to cross in one channel on one
        layer, so it routes one of four by design. Claim in the baseline that it
        routed all four and the comparison has to say the router got worse -- which
        is exactly the shape of the failure a quality change would introduce.
        """
        first = tmp_path / "baseline.json"
        run = self._run("--examples", "overconstrained", "--out", str(first))
        assert run.returncode == 0, run.stderr

        payload = json.loads(first.read_text(encoding="utf-8"))
        board = payload["boards"][0]
        board["routed"] = board["attempted"]
        board["failed"] = 0
        board["completion"] = 1.0
        first.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        again = self._run(
            "--examples", "overconstrained",
            "--out", str(tmp_path / "second.json"),
            "--compare", str(first),
            "--runtime-threshold", "1000",
            "--length-threshold", "1000",
        )
        assert again.returncode == 1, again.stdout
        assert "regression: overconstrained: completion" in again.stdout

    def test_compare_against_an_undegraded_baseline_passes(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "baseline.json"
        assert self._run(
            "--examples", "congestion", "--out", str(first)
        ).returncode == 0
        again = self._run(
            "--examples", "congestion",
            "--out", str(tmp_path / "second.json"),
            "--compare", str(first),
            # Wall clock is not deterministic even on one machine; copper is, and
            # that is the part this asserts.
            "--runtime-threshold", "1000",
        )
        assert again.returncode == 0, again.stdout

    def test_json_carries_the_comparison(self, tmp_path: Path) -> None:
        first = tmp_path / "baseline.json"
        self._run("--examples", "congestion", "--out", str(first))
        run = self._run(
            "--examples", "congestion",
            "--no-write",
            "--compare", str(first),
            "--runtime-threshold", "1000",
            "--json",
        )
        payload = json.loads(run.stdout)
        assert payload["schema"] == 1
        assert payload["comparison"]["ok"] is True
        assert payload["boards"][0]["example"] == "congestion"

    def test_an_unknown_example_is_an_input_error(self) -> None:
        run = self._run("--examples", "nonesuch", "--no-write")
        assert run.returncode == 2
        assert "nonesuch" in run.stderr

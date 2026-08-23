# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The routing benchmark: what a board costs to route, and what the result is worth.

M16c. Four separate lines of reasoning arrived here at once -- the toporouter
study's ordering recommendation, the layer-assignment candidate's measure-first
stage, the router-benchmark candidate (C6), and one of the three
:doc:`graduation conditions </roadmap>` autorouting has to meet to leave beta --
so it is built once and serves all four.

**Why it comes before the quality work.** The gEDA toporouter was not killed by a
wrong answer. It was killed by ninety seconds and thirty-five failed nets on a
269-net board, and it had no benchmark, so nobody could see it coming or tell a
regression from a hard board. Three of the five techniques that study recommends
trade runtime for quality. Without a baseline none of those trades can be judged,
which is why the roadmap's part 2 is gated on this file existing.

**What it measures, and what each number is for.**

*Wall clock per stage*, from :mod:`aipcb.route.timing`, at stage boundaries only.
This is the number the postmortem is written in and the one a user waits.

*Completion* -- connections routed over connections attempted. The single number
that says whether the router did its job. The denominator is what `aipcb route all`
reports, which means it includes the pieces a *pattern generator* laid -- a fanout
escape, a declared pair transition. Those always succeed, so a board with many of
them has a slightly flattered completion rate. Kept that way on purpose: a metric
that disagreed with the number the CLI prints for the same board would be worse than
one that is generous in a stated direction.

*Copper* -- total track length, and layer changes against a lower bound. The bound
is not a guess: a connection whose two pads share no copper layer *must* change
layer at least once, and one whose pads share a layer need not. Summing that over a
board gives the fewest layer changes any router could have made, so the excess is
what this router chose to spend. ``vias`` and ``layer_changes`` are separate numbers
on purpose -- two connections of one net can legitimately land on the same hole, so
the board carries fewer holes than the router made decisions, and only the second of
those is comparable with a bound derived per connection.

*Corridor utilization and headroom*, per layer. Every routed leg is charged to the
cuts it crosses -- the triangulation's diagonals and, since M16a, the second
diagonal of each convex adjacent triangle pair -- and the distribution of
used-over-capacity is where the board is tight. **This is pressure, not a defect.**
The field is shared and approximate while the stretcher is per-net and exact, so a
cut over 100% on a board that is DRC-clean and crossing-free means the coarse model
was pessimistic there, not that the copper is wrong.

*Layer changes without capacity pressure* -- the layer-assignment candidate's
stage 1. A via on a connection that never met a corridor above
:data:`PRESSURE_FLOOR` full is a layer change congestion did not ask for. It is a
proxy and it is named as one, but it is the measurement that would tell a future
layer-assignment pass whether it has anything to win.

*A hash of the board*, so that "the output changed" is a fact rather than an
impression. Byte-stability has its own test; this carries the fingerprint into the
committed record so a quality change has to show what it moved.

**What it is not.** It is not a test. Thresholds on wall clock are flaky by
construction -- ``--compare`` exists so a human or a CI job decides what counts as
a regression, and the CI smoke run sets its runtime threshold generously precisely
because it compares against a file measured on somebody else's machine.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aipcb.diagnostics import Report
from aipcb.route.timing import Stages

__all__ = [
    "SMOKE_EXAMPLES",
    "BenchResult",
    "BoardBench",
    "Comparison",
    "bench_board",
    "bench_examples",
    "compare",
    "render_table",
]

#: A cut this full or fuller counts as pressure the router had to respond to.
#: Half capacity, which on the bundled corpus separates the corridors nets actually
#: contend for from the open board -- and it is a stated convention rather than a
#: tuned one, because the metric it feeds is a proxy either way.
PRESSURE_FLOOR = 0.5

#: The subset CI routes on every pull request. Chosen to cover the shapes rather
#: than the sizes: `congestion` is the deliberately tight one, `routing-demo` is
#: the one with declared sketches so the check stage does real work, and
#: `led-blinker` is an ordinary two-layer board. Together they run in a handful of
#: seconds; the full corpus is minutes and stays manual.
SMOKE_EXAMPLES = ("congestion", "led-blinker", "routing-demo")

#: How much slower than the baseline a run may be before ``--compare`` calls it a
#: regression, as a percentage. The default is for comparing two runs on one
#: machine; CI passes something far larger, because it is comparing against a file
#: measured on a machine it has never met.
DEFAULT_RUNTIME_THRESHOLD = 50.0

#: How much more copper counts as a quality regression, as a percentage. Small,
#: because unlike wall clock this one is deterministic: the same input produces the
#: same length, so any movement at all is a real change in what the router decided.
DEFAULT_LENGTH_THRESHOLD = 2.0

#: The stages that are the router. ``build`` compiles the design and places it,
#: ``emit``/``stitch``/``write`` put the answer in a file; none of them is what a
#: routing change moves. They are still reported -- a user waits for all of it --
#: but the regression threshold is applied to this subset, because the first board
#: in a run also pays for loading KiCad's footprint libraries into a cache the rest
#: of the run reuses, and that one-time cost lands entirely in ``build``.
ROUTER_STAGES = (
    "fanout",
    "transitions",
    "field",
    "negotiate",
    "tighten",
    "skew",
    "invariant",
)


@dataclass(slots=True)
class LayerBench:
    """How full one layer's corridors are once the board is routed."""

    layer: str
    cuts: int
    special_cuts: int
    charged: int
    """Cuts that carry at least one route. The rest are open board."""
    utilization_p50: float
    utilization_p95: float
    utilization_max: float
    over_subscribed: int
    headroom_mm: float
    """The narrowest margin left on any charged cut. Negative means over capacity."""


@dataclass(slots=True)
class BoardBench:
    """One board's benchmark record."""

    example: str
    nets: int
    components: int
    attempted: int
    routed: int
    failed: int
    completion: float
    length_mm: float
    segments: int
    vias: int
    """Distinct holes drilled -- what the board carries, and what the CLI reports."""
    layer_changes: int
    """How many times a connection changed layer. Higher than ``vias`` when two
    connections of a net legitimately land on the same hole."""
    via_lower_bound: int
    via_excess: int
    layer_changes_without_pressure: int
    iterations: int
    converged: bool
    crossings: int
    self_crossings: int
    over_subscribed: int
    seconds: dict[str, float]
    total_seconds: float
    router_seconds: float
    """The routing stages alone -- see :data:`ROUTER_STAGES` for why that is the
    number ``--compare`` judges."""
    layers: list[LayerBench] = field(default_factory=list)
    board_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["layers"] = [asdict(layer) for layer in self.layers]
        return payload


@dataclass(slots=True)
class BenchResult:
    """Every board that was benchmarked, plus what it was benchmarked on."""

    commit: str
    recorded: str
    environment: dict[str, str]
    boards: list[BoardBench] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, Any]:
        return {
            "boards": len(self.boards),
            "attempted": sum(b.attempted for b in self.boards),
            "routed": sum(b.routed for b in self.boards),
            "failed": sum(b.failed for b in self.boards),
            "length_mm": round(sum(b.length_mm for b in self.boards), 3),
            "vias": sum(b.vias for b in self.boards),
            "layer_changes": sum(b.layer_changes for b in self.boards),
            "via_lower_bound": sum(b.via_lower_bound for b in self.boards),
            "seconds": round(sum(b.total_seconds for b in self.boards), 3),
            "router_seconds": round(sum(b.router_seconds for b in self.boards), 3),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "commit": self.commit,
            "recorded": self.recorded,
            "environment": self.environment,
            "totals": self.totals,
            "boards": [board.to_dict() for board in self.boards],
        }

    def by_example(self) -> dict[str, BoardBench]:
        return {board.example: board for board in self.boards}


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def bench_examples(
    designs: Sequence[Path], *, report: Report | None = None
) -> BenchResult:
    """Route each design in a throwaway directory and record what it cost."""
    boards = [bench_board(design, report=report) for design in designs]
    return BenchResult(
        commit=_commit(),
        recorded=datetime.now(UTC).isoformat(timespec="seconds"),
        environment=_environment(),
        boards=boards,
    )


def bench_board(design: Path, *, report: Report | None = None) -> BoardBench:
    """Route one design and measure it.

    Always into a fresh temporary directory. An incremental build preserves the
    previous run's copper and routes around it, which is the right behaviour and
    the wrong thing to benchmark: the second run of a board would measure a
    different problem from the first.
    """
    from aipcb.route.check import check_routes
    from aipcb.route.pipeline import route_design
    from aipcb.route.timing import stage

    said = report if report is not None else Report()
    stages = Stages()
    with tempfile.TemporaryDirectory(prefix="aipcb-bench-") as tmp:
        done = route_design(design, Path(tmp), said, stages=stages)
        with stage(stages, "check"):
            check_routes(done.board, done.build.netlist, Report())
        digest = hashlib.sha256(done.board_path.read_bytes()).hexdigest()

    routed = done.routed
    netlist = done.build.netlist
    attempted = len(routed.connections) + len(routed.failed)
    layers, pressure = _corridors(done)
    bound = _via_lower_bound(done)
    return BoardBench(
        example=design.parent.name,
        nets=len(netlist.nets),
        components=len(netlist.components),
        attempted=attempted,
        routed=len(routed.connections),
        failed=len(routed.failed),
        completion=round(len(routed.connections) / attempted, 4) if attempted else 1.0,
        length_mm=round(routed.total_length, 3),
        segments=done.segments,
        vias=done.vias,
        layer_changes=len(routed.vias),
        via_lower_bound=bound,
        via_excess=len(routed.vias) - bound,
        layer_changes_without_pressure=pressure,
        iterations=routed.negotiation.iterations if routed.negotiation else 0,
        converged=bool(routed.negotiation and routed.negotiation.converged),
        crossings=len(routed.crossings),
        self_crossings=len(routed.self_crossings),
        over_subscribed=sum(layer.over_subscribed for layer in layers),
        seconds=stages.to_dict(),
        total_seconds=round(stages.total, 4),
        router_seconds=round(
            sum(stages.seconds.get(name, 0.0) for name in ROUTER_STAGES), 4
        ),
        layers=layers,
        board_sha256=digest,
    )


def _via_lower_bound(done: Any) -> int:
    """The fewest vias any router could have used on this board.

    One per connection whose two pads share no copper layer, none for the rest. A
    connection that ends on a via rather than a pad -- a fanout escape, a declared
    pair transition -- is pattern-generated copper whose vias the *source* asked
    for, so it contributes nothing to a bound on the router's own choices.
    """
    from aipcb.route.geometry import edge_clearance_for
    from aipcb.route.obstacles import extract_obstacles

    netlist = done.build.netlist
    environment = extract_obstacles(
        done.board, edge_clearance=edge_clearance_for(netlist)
    )
    layers = environment.pad_layers
    bound = 0
    for connection in done.routed.connections:
        start = layers.get(connection.start)
        end = layers.get(connection.end)
        if start is None or end is None:
            continue
        if _spans(start) & _spans(end):
            continue
        bound += 1
    return bound


def _spans(layers: frozenset[str]) -> frozenset[str]:
    """A pad on ``*.Cu`` is on every layer, which is what a through-hole pad means."""
    return frozenset({"F.Cu", "B.Cu"}) if "*.Cu" in layers else layers


def _corridors(done: Any) -> tuple[list[LayerBench], int]:
    """Charge every routed leg to the cuts it crosses, and read off the pressure."""
    from aipcb.model.layout import NetClass
    from aipcb.route.costs import DEFAULT_COSTS
    from aipcb.route.field import build_field
    from aipcb.route.geometry import edge_clearance_for
    from aipcb.route.obstacles import extract_obstacles
    from aipcb.route.plan import rules_for
    from aipcb.route.stack import stack_for
    from aipcb.route.triangulate import FreeSpaceError

    netlist = done.build.netlist
    environment = extract_obstacles(
        done.board, edge_clearance=edge_clearance_for(netlist)
    )
    classes = [
        netlist.net_classes[name]
        for name in sorted({net.net_class for net in netlist.nets.values()})
        if name in netlist.net_classes
    ] or [NetClass()]
    try:
        field_ = build_field(
            environment,
            stack_for(netlist.layout, DEFAULT_COSTS),
            reference_clearance=max(c.clearance_mm for c in classes),
            reference_width=max(c.trace_width_mm for c in classes),
            via_radius=max(c.via_diameter_mm / 2 for c in classes),
            layout=netlist.layout,
            origin=netlist.layout.origin_mm if netlist.layout else (0.0, 0.0),
            special_cuts=True,
        )
    except FreeSpaceError:
        return [], 0

    # Charge everything first, then read the pressure back. Two passes and not one,
    # because "how full was the corridor this route used" is only answerable once
    # every *other* route has been charged to it as well.
    crossed: dict[int, list[tuple[str, int]]] = {}
    for connection in done.routed.connections:
        rules = rules_for(netlist, connection.net)
        demand = rules.track_width + rules.clearance
        mine: list[tuple[str, int]] = []
        for leg in connection.legs:
            layer_field = field_.layers.get(leg.layer)
            if layer_field is None:
                continue
            for edge in layer_field.cuts_crossed(leg.points):
                layer_field.used[edge] += demand
                mine.append((leg.layer, edge))
            for cut in layer_field.special_cuts_crossed(leg.points):
                layer_field.special_used[cut] += demand
        crossed[id(connection)] = mine

    peak = {
        key: max(
            (
                field_.layers[layer].used[edge] / field_.layers[layer].capacity[edge]
                for layer, edge in edges
                if field_.layers[layer].capacity[edge] > 0
            ),
            default=0.0,
        )
        for key, edges in crossed.items()
    }

    generated = {
        id(piece)
        for source in (done.routed.fanout, done.routed.transitions)
        if source is not None
        for piece in source.connections
    }
    without_pressure = sum(
        len(connection.vias)
        for connection in done.routed.connections
        if id(connection) not in generated
        and peak.get(id(connection), 0.0) < PRESSURE_FLOOR
    )

    out: list[LayerBench] = []
    for name, layer_field in sorted(field_.layers.items()):
        # Both cut families in one distribution: they are the same resource, and a
        # board is as tight as its tightest corridor whichever kind that is.
        charged = [
            (used, capacity)
            for used, capacity in (
                *zip(layer_field.used, layer_field.capacity, strict=True),
                *zip(
                    layer_field.special_used,
                    layer_field.special_capacity,
                    strict=True,
                ),
            )
            if used > 0 and capacity > 0
        ]
        ratios = [used / capacity for used, capacity in charged]
        margins = [capacity - used for used, capacity in charged]
        out.append(
            LayerBench(
                layer=name,
                cuts=len(layer_field.capacity),
                special_cuts=len(layer_field.special_capacity),
                charged=len(charged),
                utilization_p50=round(_percentile(ratios, 0.50), 4),
                utilization_p95=round(_percentile(ratios, 0.95), 4),
                utilization_max=round(max(ratios), 4) if ratios else 0.0,
                over_subscribed=len(layer_field.over_subscribed())
                + len(layer_field.over_subscribed_special()),
                headroom_mm=round(min(margins), 4) if margins else 0.0,
            )
        )
    return out, without_pressure


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, so the answer is always a value that was actually measured."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


def _commit() -> str:
    """Which commit this was measured on, and whether the tree was clean.

    The ``-dirty`` suffix matters more here than in most places: a baseline taken
    mid-milestone is measured on a working tree, and a file that named a commit
    whose code it had never run would be the wrong kind of record.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        return "unknown"
    if not head:
        return "unknown"
    return f"{head}-dirty" if dirty else head


def _environment() -> dict[str, str]:
    """What the numbers were measured on, because wall clock without it is noise."""
    import shapely

    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
    }


# ---------------------------------------------------------------------------
# comparing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Comparison:
    """What changed between two benchmark runs, and which changes are regressions."""

    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    """Movement that is neither, and still wants saying -- a changed board hash."""
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.regressions

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "changes": self.changes,
            "missing": self.missing,
        }


def compare(
    baseline: BenchResult,
    current: BenchResult,
    *,
    runtime_threshold: float = DEFAULT_RUNTIME_THRESHOLD,
    length_threshold: float = DEFAULT_LENGTH_THRESHOLD,
    expect_all: bool = True,
) -> Comparison:
    """Diff two runs and say which differences are regressions.

    The asymmetry between the two thresholds is the point. Copper length is a
    deterministic function of the input, so a 2% default is not a tolerance for
    noise -- it is a statement about how much of a quality change is worth stopping
    for. Wall clock is not deterministic and never will be, so its threshold is
    coarse and the CI job's is coarser still.

    ``expect_all`` says whether this run was meant to cover the baseline's whole
    corpus. A subset run -- the CI smoke run is one -- is not missing the eight
    boards it never intended to route, and listing them would bury the four lines
    that matter.
    """
    outcome = Comparison()
    before, after = baseline.by_example(), current.by_example()
    if expect_all:
        for name in sorted(before):
            if name not in after:
                outcome.missing.append(f"{name}: not in this run")
    for name in sorted(after):
        new = after[name]
        old = before.get(name)
        if old is None:
            outcome.changes.append(f"{name}: not in the baseline")
            continue
        _compare_board(name, old, new, runtime_threshold, length_threshold, outcome)
    return outcome


def _compare_board(
    name: str,
    old: BoardBench,
    new: BoardBench,
    runtime_threshold: float,
    length_threshold: float,
    outcome: Comparison,
) -> None:
    if new.completion < old.completion - 1e-9:
        outcome.regressions.append(
            f"{name}: completion {old.completion:.1%} -> {new.completion:.1%} "
            f"({old.routed}/{old.attempted} -> {new.routed}/{new.attempted})"
        )
    elif new.completion > old.completion + 1e-9:
        outcome.improvements.append(
            f"{name}: completion {old.completion:.1%} -> {new.completion:.1%}"
        )

    for label, was, now in (
        ("crossings", old.crossings, new.crossings),
        ("self-crossings", old.self_crossings, new.self_crossings),
    ):
        if now > was:
            outcome.regressions.append(f"{name}: {label} {was} -> {now}")
        elif now < was:
            outcome.improvements.append(f"{name}: {label} {was} -> {now}")

    if old.length_mm > 0:
        moved = (new.length_mm - old.length_mm) / old.length_mm * 100
        if moved > length_threshold:
            outcome.regressions.append(
                f"{name}: copper {old.length_mm:.1f} -> {new.length_mm:.1f} mm "
                f"(+{moved:.1f}%)"
            )
        elif moved < -length_threshold:
            outcome.improvements.append(
                f"{name}: copper {old.length_mm:.1f} -> {new.length_mm:.1f} mm "
                f"({moved:.1f}%)"
            )

    for label, was, now in (
        ("vias", old.vias, new.vias),
        ("layer changes", old.layer_changes, new.layer_changes),
    ):
        if now > was:
            outcome.regressions.append(f"{name}: {label} {was} -> {now}")
        elif now < was:
            outcome.improvements.append(f"{name}: {label} {was} -> {now}")

    if old.router_seconds > 0:
        moved = (new.router_seconds - old.router_seconds) / old.router_seconds * 100
        if moved > runtime_threshold:
            outcome.regressions.append(
                f"{name}: routing {old.router_seconds:.2f} -> "
                f"{new.router_seconds:.2f} s (+{moved:.0f}%)"
            )
        elif moved < -runtime_threshold:
            outcome.improvements.append(
                f"{name}: routing {old.router_seconds:.2f} -> "
                f"{new.router_seconds:.2f} s ({moved:.0f}%)"
            )

    if old.board_sha256 and new.board_sha256 != old.board_sha256:
        outcome.changes.append(
            f"{name}: the board changed ({old.board_sha256[:12]} -> "
            f"{new.board_sha256[:12]})"
        )


# ---------------------------------------------------------------------------
# reading and writing
# ---------------------------------------------------------------------------


def load(path: Path) -> BenchResult:
    """Read a results file, refusing one this version cannot read.

    A baseline outlives the code that wrote it, so a field added or renamed here
    turns an old file into a `TypeError` deep inside a dataclass constructor. Better
    to say which field and suggest the fix, which is always the same: re-measure.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema = payload.get("schema")
    if schema != 1:
        raise ValueError(
            f"{path} is a schema {schema} results file and this build reads "
            "schema 1; re-measure it with `aipcb bench`"
        )
    boards = []
    for board in payload.get("boards", ()):
        try:
            boards.append(
                BoardBench(
                    **{
                        **{k: v for k, v in board.items() if k != "layers"},
                        "layers": [
                            LayerBench(**layer) for layer in board.get("layers", ())
                        ],
                    }
                )
            )
        except TypeError as exc:
            raise ValueError(
                f"{path} does not match this build's metrics "
                f"({exc}); re-measure it with `aipcb bench`"
            ) from exc
    return BenchResult(
        commit=payload.get("commit", "unknown"),
        recorded=payload.get("recorded", ""),
        environment=payload.get("environment", {}),
        boards=boards,
    )


def write(result: BenchResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("board", "example", 16),
    ("nets", "nets", 5),
    ("conn", "attempted", 5),
    ("done", "routed", 5),
    ("%", "completion", 6),
    ("copper mm", "length_mm", 10),
    ("vias", "vias", 5),
    ("hops", "layer_changes", 5),
    ("min", "via_lower_bound", 5),
    ("iters", "iterations", 6),
    ("over", "over_subscribed", 5),
    ("route s", "router_seconds", 8),
    ("total s", "total_seconds", 8),
)


def render_table(result: BenchResult) -> str:
    """The human form. Fixed-width, because it is read in a terminal and in a diff."""
    head = "  ".join(title.rjust(width) for title, _, width in _COLUMNS)
    rule = "  ".join("-" * width for _, _, width in _COLUMNS)
    rows = [head, rule]
    for board in result.boards:
        cells = []
        for _, attribute, width in _COLUMNS:
            value = getattr(board, attribute)
            if attribute == "completion":
                text = f"{value:.0%}"
            elif attribute in {"total_seconds", "router_seconds"}:
                text = f"{value:.2f}"
            elif isinstance(value, float):
                text = f"{value:.3f}"
            else:
                text = str(value)
            cells.append(text.rjust(width))
        rows.append("  ".join(cells))
    totals = result.totals
    rows.append(rule)
    rows.append(
        f"{len(result.boards)} boards, {totals['routed']}/{totals['attempted']} "
        f"connections, {totals['length_mm']:.1f} mm of copper, "
        f"{totals['vias']} vias, {totals['layer_changes']} layer changes "
        f"(bound {totals['via_lower_bound']}), "
        f"{totals['router_seconds']:.1f} s routing of {totals['seconds']:.1f} s total"
    )
    return "\n".join(rows)


def render_stages(result: BenchResult) -> str:
    """Where the time went, per board. The table the postmortem asked for."""
    names: list[str] = []
    for board in result.boards:
        for stage_name in board.seconds:
            if stage_name not in names:
                names.append(stage_name)
    head = "board".ljust(16) + "  " + "  ".join(n.rjust(9) for n in names)
    rows = [head, "-" * len(head)]
    for board in result.boards:
        cells = [f"{board.seconds.get(n, 0.0):9.3f}" for n in names]
        rows.append(board.example.ljust(16) + "  " + "  ".join(cells))
    return "\n".join(rows)


def render_comparison(outcome: Comparison) -> str:
    lines: list[str] = []
    for label, entries in (
        ("regression", outcome.regressions),
        ("improvement", outcome.improvements),
        ("change", outcome.changes),
        ("missing", outcome.missing),
    ):
        for entry in entries:
            lines.append(f"{label}: {entry}")
    if not lines:
        return "no differences beyond the thresholds"
    return "\n".join(lines)


def resolve(names: Iterable[str] | None, root: Path) -> list[Path]:
    """Which designs to benchmark. Every bundled example unless told otherwise."""
    available = {p.parent.name: p for p in sorted((root / "examples").glob("*/design.yaml"))}
    if names is None:
        return list(available.values())
    chosen: list[Path] = []
    for name in names:
        design = available.get(name)
        if design is None:
            raise KeyError(
                f"no example named {name!r}; available: {', '.join(sorted(available))}"
            )
        chosen.append(design)
    return chosen


def repository_root() -> Path:
    """The checkout this is running from, so `bench/results/` lands in the right place."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "examples").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()  # pragma: no cover - an installed package with no checkout


def default_output(result: BenchResult, root: Path) -> Path:
    return root / "bench" / "results" / f"{result.commit}.json"

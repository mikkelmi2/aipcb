"""The batch: slice every pair, solve the ones that changed, report what came back.

Sequential by design. A single openEMS run already saturates the machine -- the
solver reports 700-1300 MCells/s on sixteen cores and scales with them -- so running
two pairs at once splits the same cores between them and finishes no sooner. That is
a measurement, not an assumption; the M12 report carries the numbers. What actually
makes a re-run fast is the cache, not concurrency.

Failures are per pair. One diverging or unroutable pair is a line in the report and
the batch carries on, because the value of a batch is the eleven pairs that worked.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aipcb.compile.export import export_board
from aipcb.diagnostics import Report
from aipcb.highspeed import target_for
from aipcb.kicad.sexpr import SNode, dump
from aipcb.model.simulation import ResolvedSimulation
from aipcb.netlist import Netlist
from aipcb.si import IMAGE
from aipcb.si.inputs import write_inputs
from aipcb.si.pairs import LogicalPair, logical_pairs
from aipcb.si.results import Metrics, analyse, read_sparameters, write_touchstone
from aipcb.si.runner import (
    ContainerBusy,
    ContainerMissing,
    RunOutcome,
    container_digest,
    find_container,
    nets_in_gerbers,
    run_gerber2ems,
    slice_digest,
)
from aipcb.si.slice import Slice, SliceError, build_slice

__all__ = ["PairResult", "SimulationBatch", "simulate_pairs"]

#: Reference impedance for a port when the pair's class declares no target. Fifty
#: ohms is the instrument convention and, at a hundred ohms differential, also
#: roughly right -- but a port far from the line's own impedance rings, so a class
#: with a target gets half of it instead.
DEFAULT_PORT_OHM = 50.0


@dataclass(slots=True)
class PairResult:
    """One pair's trip through the batch, whatever happened to it."""

    pair: LogicalPair
    status: str
    """``simulated``, ``cached``, ``failed`` or ``not-routed``."""
    directory: Path
    seconds: float = 0.0
    digest: str = ""
    sliced: Slice | None = None
    outcome: RunOutcome | None = None
    metrics: Metrics | None = None
    message: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "pair": self.pair.name,
            "net_class": self.pair.net_class,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "digest": self.digest[:16],
            "directory": str(self.directory),
            "message": self.message,
        }
        if self.sliced is not None:
            payload["slice"] = self.sliced.to_dict()
        if self.outcome is not None:
            payload["run"] = self.outcome.to_dict()
        if self.metrics is not None:
            payload["metrics"] = self.metrics.to_dict()
        return payload


@dataclass(slots=True)
class SimulationBatch:
    """Everything a run produced, and what it cost."""

    design: str
    directory: Path
    results: list[PairResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(pair, why)`` for pairs that were never sliced."""
    total_seconds: float = 0.0
    image: str = IMAGE
    image_digest: str = ""
    runtime: str = ""

    def summary(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return {
            "design": self.design,
            "directory": str(self.directory),
            "image": self.image,
            "image_digest": self.image_digest,
            "runtime": Path(self.runtime).name if self.runtime else "",
            "total_seconds": round(self.total_seconds, 2),
            "counts": dict(sorted(counts.items())),
            "pairs": [r.to_dict() for r in self.results],
            "not_simulated": [
                {"pair": name, "why": why} for name, why in sorted(self.skipped)
            ],
        }

    def to_manifest(self) -> dict[str, object]:
        return self.summary()


def _port_impedance(netlist: Netlist, pair: LogicalPair) -> tuple[float, float | None]:
    """``(per-port reference, differential target)`` for one pair's class.

    A port terminated far from the line's own impedance makes the input impedance
    ring, and reading a ringing curve is what phase 0 found gave three different
    answers for one geometry. Terminating each port in half the declared
    differential target flattens it, so the number that comes back is the line's,
    not the termination's.
    """
    target = target_for(netlist, pair.net_class)
    if target is None or not target.target_ohm:
        return DEFAULT_PORT_OHM, None
    return round(target.target_ohm / 2, 4), target.target_ohm


def simulate_pairs(
    board: SNode,
    netlist: Netlist,
    out_dir: Path,
    report: Report,
    *,
    only: tuple[str, ...] = (),
    net_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    timeout_s: int | None = None,
    image: str = IMAGE,
    progress: Callable[[PairResult], None] | None = None,
    geometric_skew: dict[str, float] | None = None,
) -> SimulationBatch:
    """Slice, solve and analyse every selected pair on a routed board."""
    out_dir.mkdir(parents=True, exist_ok=True)
    batch = SimulationBatch(design=netlist.name, directory=out_dir)
    started = time.monotonic()

    runtime = ""
    if not dry_run:
        try:
            runtime = find_container()
            batch.image_digest = container_digest(runtime, image)
        except ContainerMissing as exc:
            report.error(
                "si-toolchain-missing",
                str(exc),
                hint="signal-integrity simulation needs a container runtime and the "
                "pinned gerber2ems image; ADR 0011 records how it was built. Nothing "
                "else in aipcb needs either.",
            )
            return batch
    batch.runtime = runtime

    for pair in logical_pairs(netlist):
        if only and pair.name not in only:
            continue
        if net_class is not None and pair.net_class != net_class:
            continue
        result = _one_pair(
            board,
            netlist,
            pair,
            out_dir,
            report,
            runtime=runtime,
            image=image,
            force=force,
            dry_run=dry_run,
            timeout_s=timeout_s,
            geometric_skew=geometric_skew,
        )
        if result is None:
            continue
        batch.results.append(result)
        if progress is not None:
            progress(result)

    for pair in logical_pairs(netlist):
        if any(r.pair.name == pair.name for r in batch.results):
            continue
        if only and pair.name not in only:
            batch.skipped.append((pair.name, "not selected on the command line"))
        elif net_class is not None and pair.net_class != net_class:
            batch.skipped.append((pair.name, f"not in net class {net_class}"))

    batch.total_seconds = time.monotonic() - started
    (out_dir / "manifest.json").write_text(
        json.dumps(batch.to_manifest(), indent=2) + "\n", encoding="utf-8"
    )
    return batch


def _one_pair(
    board: SNode,
    netlist: Netlist,
    pair: LogicalPair,
    out_dir: Path,
    report: Report,
    *,
    runtime: str,
    image: str,
    force: bool,
    dry_run: bool,
    timeout_s: int | None,
    geometric_skew: dict[str, float] | None = None,
) -> PairResult | None:
    work = out_dir / pair.name
    settings = netlist.simulation.for_class(pair.net_class)
    port_ohm, target = _port_impedance(netlist, pair)

    try:
        sliced = build_slice(board, netlist, pair, settings, port_impedance_ohm=port_ohm)
    except SliceError as exc:
        report.warning(
            exc.code, exc.message, hint=exc.hint or None, path=pair.source_path
        )
        return PairResult(
            pair=pair,
            status="not-routed",
            directory=work,
            message=exc.message,
            hint=exc.hint,
        )

    stackup = netlist.layout.stackup if netlist.layout else None
    if stackup is None:
        report.warning(
            "si-no-stackup",
            f"{pair.name} cannot be simulated: the design declares no `layout.stackup`",
            path=pair.source_path,
        )
        return PairResult(pair=pair, status="failed", directory=work,
                          message="no stackup declared")

    # Everything the digest is computed from is written *before* the export, so a
    # cache hit costs a slice and three small files rather than a zone fill and five
    # runs of kicad-cli. On eleven pairs that is the difference between a re-run
    # taking a minute and taking nothing at all.
    work.mkdir(parents=True, exist_ok=True)
    fab = work / "fab"
    (work / "slice.kicad_pcb").write_text(dump(sliced.board), encoding="utf-8")
    write_inputs(work, sliced, stackup, settings)
    (work / "slice.json").write_text(
        json.dumps(sliced.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    digest = slice_digest(work)
    cached = work / "result.json"
    if cached.exists() and not force and not dry_run:
        stored = json.loads(cached.read_text(encoding="utf-8"))
        if stored.get("digest") == digest:
            metrics = _analyse(
                work, pair, settings, port_ohm, target, sliced,
                netlist, geometric_skew,
            )
            # The metrics are recomputed on every hit, because the *analysis* can
            # change when the solver's output has not -- M13.5 added a caveat that
            # a layer-spanning link now carries, and every cached `result.json` on
            # this machine was still claiming the old one. So the file is refreshed
            # too, and it stops being a record that quietly disagrees with what the
            # tool would say today. `digest` and `run` are the run and do not move.
            _write_result(cached, digest, stored.get("seconds", 0.0),
                          stored.get("run"), metrics)
            return PairResult(
                pair=pair, status="cached", directory=work, digest=digest,
                sliced=sliced, metrics=metrics, seconds=0.0,
                message="unchanged since the last run",
            )

    # Plotted output from an earlier run goes first. `stackup.json` stays: it is in
    # `fab/` because that is where gerber2ems looks for it, not because kicad-cli put
    # it there, and it was written above with the rest of the digest's inputs.
    for stale in (*fab.glob("*.gbr"), *fab.glob("*.drl"), *fab.glob("*pos.csv")):
        stale.unlink()
    exported = export_board(work / "slice.kicad_pcb", fab, netlist, report)
    if not exported.ok:
        return PairResult(
            pair=pair, status="failed", directory=work, sliced=sliced, digest=digest,
            message="the slice could not be exported",
        )

    # Read the export back before spending minutes on it. A slice whose copper lost
    # its net attributes meshes coarsely and comes back a short circuit, at a clean
    # exit code -- the third silent failure of this integration, after the placement
    # file's name and the drill file's frame.
    seen = nets_in_gerbers(fab)
    missing = [net for net in sliced.pair.nets if net not in seen]
    if missing:
        message = (
            f"the exported Gerbers carry no net attribute for {', '.join(missing)}, "
            "so the mesh generator would not know which copper to resolve"
        )
        report.error("si-slice-nets-lost", f"{pair.name}: {message}",
                     path=pair.source_path)
        return PairResult(pair=pair, status="failed", directory=work, sliced=sliced,
                          digest=digest, message=message)

    # A dry run stops here: everything the solver reads has been produced and read
    # back, which is the half of the pipeline worth checking without a container.
    if dry_run:
        return PairResult(pair=pair, status="sliced", directory=work, digest=digest,
                          sliced=sliced)

    drill = next(iter(sorted(fab.glob("*-PTH.drl"))), None)
    expect_vias = _hole_count(drill) if drill else 0
    try:
        outcome = run_gerber2ems(
            work,
            runtime=runtime,
            image=image,
            timeout_s=timeout_s if timeout_s is not None else settings.timeout_s,
            expect_ports=len(sliced.ports),
            expect_vias=expect_vias,
            log_path=work / "run.log",
        )
    except ContainerBusy as exc:
        # M13d. Refusing is the answer: this directory already has a writer, and
        # the chain's own worst simulation hour was two of them.
        report.error(
            "si-directory-busy",
            f"{pair.name}: {exc}",
            hint="a container outliving the client that started it is what M13d "
            "closed; one left over from before that is reaped by hand",
            path=pair.source_path,
        )
        return PairResult(pair=pair, status="failed", directory=work, digest=digest,
                          sliced=sliced, message=str(exc))
    if not outcome.ok:
        report.warning(
            "si-simulation-failed",
            f"{pair.name}: {outcome.message}",
            hint=f"the full solver log is at {work / 'run.log'}",
            path=pair.source_path,
        )
        return PairResult(pair=pair, status="failed", directory=work, digest=digest,
                          sliced=sliced, outcome=outcome, seconds=outcome.seconds,
                          message=outcome.message)

    metrics = _analyse(
        work, pair, settings, port_ohm, target, sliced, netlist, geometric_skew,
    )
    _write_result(cached, digest, outcome.seconds, outcome.to_dict(), metrics)
    for note in outcome.warnings:
        report.warning("si-convergence", f"{pair.name}: {note}", path=pair.source_path)
    return PairResult(
        pair=pair, status="simulated", directory=work, digest=digest, sliced=sliced,
        outcome=outcome, metrics=metrics, seconds=outcome.seconds,
    )


def _write_result(
    path: Path,
    digest: str,
    seconds: float,
    run: dict[str, Any] | None,
    metrics: Metrics | None,
) -> None:
    """Write a pair's `result.json`: the run as it happened, the metrics as read now."""
    path.write_text(
        json.dumps(
            {
                "digest": digest,
                "seconds": round(seconds, 2),
                "run": run,
                "metrics": metrics.to_dict() if metrics else None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _geometric_skew(
    pair: LogicalPair, measured: dict[str, float] | None
) -> float | None:
    """What M11e measured on the copper for this logical link, worst-case.

    A logical link can be more than one declared pair -- `PCIE_TX` and
    `PCIE_TX_C` are one run through two capacitors -- and the router measures each
    declared pair separately. The link's skew is the *worst* of them: the two sides
    of a series capacitor are one conductor, and a mismatch on either side is a
    mismatch on the link.
    """
    if not measured:
        return None
    found = [
        measured[key]
        for declared in pair.declared
        if (key := "+".join(sorted(declared))) in measured
    ]
    return max(found) if found else measured.get(pair.name)


def _hole_count(drill: Path) -> int:
    """How many holes the plated drill file actually contains.

    Counted the way gerber2ems counts them -- a coordinate line -- so that "it saw
    every via" is a comparison of like with like.
    """
    count = 0
    for line in drill.read_text(encoding="utf-8").splitlines():
        if line[:1] == "X" and "Y" in line:
            count += 1
    return count


def _analyse(
    work: Path,
    pair: LogicalPair,
    settings: ResolvedSimulation,
    port_ohm: float,
    target: float | None,
    sliced: Slice,
    netlist: Netlist | None = None,
    geometric_skew: dict[str, float] | None = None,
) -> Metrics | None:
    sp = read_sparameters(work / "ems" / "simulation", ports=len(sliced.ports))
    if not sp.frequencies:
        return None
    write_touchstone(sp, work / f"{pair.name}.s{len(sliced.ports)}p", port_ohm)
    return analyse(
        sp,
        pair=pair.name,
        net_class=pair.net_class,
        port_impedance_ohm=port_ohm,
        target_ohm=target,
        settings=settings,
        length_mm=sliced.conductor_length_mm,
        geometric_skew_mm=_geometric_skew(pair, geometric_skew),
        slice_skew_mm=sliced.slice_skew_mm,
        spans_layers=sliced.spans_layers,
        max_skew_mm=(
            netlist.net_classes[pair.net_class].max_skew_mm
            if netlist is not None and pair.net_class in netlist.net_classes
            else None
        ),
    )

"""``aipcb simulate`` -- electromagnetic simulation of the controlled-impedance pairs.

Deliberately not part of ``check``. Two reasons, and only one of them is runtime:
a pair costs a minute or two where the whole of ``check`` costs seconds, and -- more
importantly -- what comes back is engineering judgement rather than a gate. An
impedance eight percent off target is a number to think about; it is not a
correctness failure the way a shorted net is.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from aipcb.diagnostics import AipcbError, Report
from aipcb.si.runner import DEFAULT_TIMEOUT_S
from aipcb.source import SourceError

if TYPE_CHECKING:  # pragma: no cover - imported only for annotations
    from aipcb.netlist import Netlist
    from aipcb.si.run import PairResult, SimulationBatch

__all__ = ["simulate"]

DesignArg = Annotated[Path, typer.Argument(help="Path to the design file.", dir_okay=False)]


def register(app: typer.Typer) -> None:
    """Attach the command to the top-level app."""
    app.command()(simulate)


def simulate(
    design: DesignArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Where slices and results go. Defaults to "
                     "`<design dir>/out/si`."),
    ] = None,
    net: Annotated[
        list[str] | None,
        typer.Option("--net", help="Simulate only this pair, by either net's name. "
                     "Repeatable."),
    ] = None,
    net_class: Annotated[
        str | None,
        typer.Option("--net-class", help="Simulate only pairs in this net class."),
    ] = None,
    board: Annotated[
        Path | None,
        typer.Option("--board", help="Use this already-routed `.kicad_pcb` instead of "
                     "building and routing the design again."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-simulate even when nothing changed.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Generate the slices and the solver inputs, "
                     "then stop. No container is started."),
    ] = False,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds one pair may take.", min=1)
    ] = DEFAULT_TIMEOUT_S,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Simulate each differential pair and report impedance, return and insertion loss."""
    from aipcb.kicad.sexpr import parse
    from aipcb.route.pipeline import route_design
    from aipcb.si.run import simulate_pairs

    report = Report()
    target = out or (design.parent / "out" / "si")

    try:
        if board is not None:
            from aipcb.compile.build import compile_netlist

            netlist = compile_netlist(design, report)
            if not report.ok:
                raise AipcbError(f"{design}: validation failed", report)
            parsed = parse(board.read_text(encoding="utf-8"))
            # A board handed in on the command line carries no routing record, so
            # there is no geometric skew to compare the fit against. Said here
            # rather than left as an empty column nobody can explain.
            geometric_skew = {}
        else:
            # `aipcb export` builds into a throwaway directory and therefore ships a
            # board with no tracks on it (ADR 0011, gap 10). Simulation needs the
            # copper, so it routes for itself unless handed a board that already has.
            with tempfile.TemporaryDirectory(prefix="aipcb-si-") as tmp:
                done = route_design(design, Path(tmp), report)
                netlist = done.build.netlist
                parsed = done.board
                # M11e's geometric skew, so M13c's frequency-domain fit has
                # something to be checked against. Two verification layers
                # agreeing is worth more than either alone; the point of putting
                # them side by side is that they can disagree.
                geometric_skew = dict(done.routed.skew)
    except SourceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AipcbError as exc:
        typer.echo(exc.report.render(color=sys.stdout.isatty()), err=True)
        raise typer.Exit(1) from exc

    selected = _select(netlist, net)
    if net and not selected:
        typer.echo(f"error: no differential pair matches {', '.join(net)}", err=True)
        raise typer.Exit(2)

    def announce(result: PairResult) -> None:
        if as_json:
            return
        line = f"  {result.pair.name:<24} {result.status}"
        if result.seconds:
            line += f"  {result.seconds:6.1f} s"
        if result.metrics is not None:
            line += f"  Zdiff {result.metrics.impedance_ohm:6.1f} ohm"
            if result.metrics.target_ohm and result.metrics.deviation is not None:
                line += (
                    f"  (target {result.metrics.target_ohm:.0f}, "
                    f"{result.metrics.deviation:+.1%})"
                )
        typer.echo(line)

    if not as_json:
        typer.echo(f"simulating {netlist.name} into {target}")
    batch = simulate_pairs(
        parsed,
        netlist,
        target,
        report,
        only=selected,
        net_class=net_class,
        force=force,
        dry_run=dry_run,
        timeout_s=timeout,
        progress=announce,
        geometric_skew=geometric_skew,
    )
    _emit_findings(batch, report)

    if as_json:
        payload = report.to_dict()
        payload["simulation"] = batch.summary()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(_render(batch))
    raise typer.Exit(1 if not report.ok else 0)


def _select(netlist: Netlist, net: list[str] | None) -> tuple[str, ...]:
    """Turn ``--net`` names into pair names, accepting either half of a pair."""
    from aipcb.si.pairs import logical_pairs

    if not net:
        return ()
    wanted = {name.strip() for name in net}
    return tuple(
        pair.name
        for pair in logical_pairs(netlist)
        if wanted & set(pair.nets) or pair.name in wanted
    )


def _emit_findings(batch: SimulationBatch, report: Report) -> None:
    """One finding per metric that did not pass, pointing at the pair's source lines."""
    for result in batch.results:
        metrics = result.metrics
        if metrics is None:
            continue
        path = result.pair.source_path
        for note in metrics.notes:
            code = "si-not-physical" if not metrics.usable and "|Sdd21|" in note else "si-caveat"
            report.warning(code, f"{metrics.pair}: {note}", path=path)
        if metrics.verdicts.get("impedance") == "warn" and metrics.target_ohm:
            report.warning(
                "si-impedance",
                f"{metrics.pair} simulates at {metrics.impedance_ohm:.1f} ohm "
                f"differential against a declared {metrics.target_ohm:.0f} ohm "
                f"({metrics.deviation or 0:+.1%})",
                hint="the threshold is an engineering default, not a standard; "
                "simulation validates the layout, not what the fabricator presses",
                path=path,
                pair=metrics.pair,
                impedance_ohm=round(metrics.impedance_ohm, 2),
                target_ohm=metrics.target_ohm,
            )
        if metrics.verdicts.get("return_loss") == "warn":
            report.warning(
                "si-return-loss",
                f"{metrics.pair} reflects {metrics.worst_return_loss_db:.1f} dB at "
                f"{metrics.worst_return_loss_hz / 1e9:.2f} GHz",
                path=path,
                pair=metrics.pair,
            )
        if metrics.verdicts.get("mode_conversion") == "warn-low-confidence":
            report.warning(
                "si-mode-conversion",
                f"{metrics.pair} converts {metrics.mode_conversion_db:.1f} dB of its "
                f"differential signal to common mode at "
                f"{metrics.mode_conversion_hz / 1e9:.2f} GHz -- **low confidence as "
                "a skew finding**",
                hint="this is a worst-in-band maximum, and on the boards measured so "
                "far what it reads is the mode-conversion floor rather than the "
                "skew: M12 found it flagging the three best-matched pairs on an "
                "eleven-link board and passing both mismatched ones. The skew "
                "verdict is `si-skew-fit`, which reads the same data across "
                "frequency",
                path=path,
                pair=metrics.pair,
            )
        if metrics.verdicts.get("skew_fit") == "warn" and metrics.skew_fit is not None:
            geometric = (
                f"{metrics.geometric_skew_mm:.3f} mm"
                if metrics.geometric_skew_mm is not None
                else "not measured"
            )
            report.warning(
                "si-skew-fit",
                f"{metrics.pair}: the mode-conversion curve fits "
                f"{metrics.skew_fit.delay_ps:.2f} ps of intra-pair delay, which at "
                f"this pair's measured {metrics.ps_per_mm or 0:.2f} ps/mm is "
                f"{metrics.fitted_skew_mm or 0:.3f} mm against a "
                f"{metrics.max_skew_mm:g} mm budget (M11e measured {geometric} on "
                "the copper)",
                hint="fitted against |sin(pi f dt)| across the band rather than "
                "read off a single worst point, so the flat mode-conversion floor "
                "is a fitted term rather than the answer. A warning and not an "
                "error: promoting it needs the fit's false-positive behaviour "
                "measured first",
                path=path,
                pair=metrics.pair,
                fitted_ps=round(metrics.skew_fit.delay_ps, 3),
                fitted_mm=round(metrics.fitted_skew_mm or 0.0, 4),
                geometric_mm=metrics.geometric_skew_mm,
            )
        if metrics.verdicts.get("insertion_loss") == "warn":
            worst = min(metrics.insertion_loss_db.items(), key=lambda kv: kv[1])
            report.warning(
                "si-insertion-loss",
                f"{metrics.pair} loses {abs(worst[1]):.1f} dB at {worst[0]}",
                path=path,
                pair=metrics.pair,
            )


def _render(batch: SimulationBatch) -> str:
    """The human summary: one line per pair, then the totals."""
    rows: list[str] = []
    counts = batch.summary()["counts"]
    assert isinstance(counts, dict)
    for result in batch.results:
        metrics = result.metrics
        if metrics is None:
            rows.append(f"  {result.pair.name:<24} {result.status}: {result.message}")
            continue
        verdict = ", ".join(f"{k}={v}" for k, v in sorted(metrics.verdicts.items()))
        rows.append(
            f"  {result.pair.name:<24} Zdiff {metrics.impedance_ohm:6.1f} ohm "
            f"[{metrics.impedance_min_ohm:.0f}-{metrics.impedance_max_ohm:.0f}]  "
            f"RL {metrics.worst_return_loss_db:6.1f} dB  {verdict}"
        )
        # The two verification layers on one line, because the interesting thing
        # about them is whether they agree (M13c).
        if metrics.skew_fit is not None and metrics.fitted_skew_mm is not None:
            geometric = (
                f"{metrics.geometric_skew_mm:.3f}"
                if metrics.geometric_skew_mm is not None
                else "    -"
            )
            rows.append(
                f"  {'':<24}   skew: fitted {metrics.skew_fit.delay_ps:6.2f} ps "
                f"= {metrics.fitted_skew_mm:.3f} mm, geometric {geometric} mm, "
                f"floor {metrics.skew_fit.floor_db:.1f} dB "
                f"(+{metrics.skew_fit.peak_over_floor_db:.1f} dB), "
                f"fit residual {metrics.skew_fit.residual_db:.1f} dB"
            )
    for name, why in batch.skipped:
        rows.append(f"  {name:<24} not simulated: {why}")
    tally = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing"
    rows.append(
        f"{tally} in {batch.total_seconds:.1f} s; results in {batch.directory}"
    )
    return "\n".join(rows)

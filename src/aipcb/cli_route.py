"""The ``aipcb route`` commands."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from aipcb.diagnostics import AipcbError, Report
from aipcb.route.plan import DEFAULT_CONGESTION
from aipcb.source import SourceError

if TYPE_CHECKING:  # pragma: no cover - imported only for annotations
    from aipcb.compile.build import BuildResult
    from aipcb.kicad.sexpr import SNode

route_app = typer.Typer(
    name="route",
    help="Check and generate topological routing.",
    no_args_is_help=True,
    add_completion=False,
)

DesignArg = Annotated[Path, typer.Argument(help="Path to the design file.", dir_okay=False)]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _build(
    design: Path, out: Path, report: Report
) -> tuple[BuildResult, Path, SNode]:
    """Build a design and hand back the parsed board alongside it."""
    from aipcb.compile.build import build_design
    from aipcb.kicad.sexpr import parse

    result = build_design(design, out_dir=out, report=report)
    board_path = next(p for p in result.written if p.suffix == ".kicad_pcb")
    return result, board_path, parse(board_path.read_text(encoding="utf-8"))


@route_app.command("check")
def route_check(design: DesignArg, as_json: JsonOpt = False) -> None:
    """Verify that every route topology can actually be built on this placement."""
    from aipcb.route.check import check_routes

    report = Report()
    try:
        with tempfile.TemporaryDirectory(prefix="aipcb-route-") as tmp:
            result, _, board = _build(design, Path(tmp), report)
            outcome = check_routes(board, result.netlist, report)
    except SourceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AipcbError as exc:
        typer.echo(exc.report.render(color=sys.stdout.isatty()), err=True)
        raise typer.Exit(1) from exc

    if as_json:
        payload = report.to_dict()
        payload["routes"] = outcome.to_dict()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty()))
        checked = len(outcome.realizable) + len(outcome.unrealizable)
        if checked == 0:
            typer.echo("no route topologies declared under `layout.routes`")
        else:
            typer.echo(
                f"{len(outcome.realizable)}/{checked} route topologies are realizable"
            )
    raise typer.Exit(1 if not report.ok else 0)


@route_app.command("all")
def route_all(
    design: DesignArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output directory. Defaults to the design's."),
    ] = None,
    layers: Annotated[
        str | None,
        typer.Option(
            "--layers",
            help="Comma-separated copper layers to route on. Defaults to every "
            "signal layer in the stackup.",
        ),
    ] = None,
    congestion: Annotated[
        float,
        typer.Option(
            "--congestion",
            help="How hard to avoid narrow gaps. 0 routes purely for length.",
            min=0.0,
        ),
    ] = DEFAULT_CONGESTION,
    as_json: JsonOpt = False,
) -> None:
    """Build a design and route it, writing tracks into the board."""
    from aipcb.kicad.sexpr import dump
    from aipcb.route.emit import attach_copper, drop_generated, generated_uuids
    from aipcb.route.plan import RoutedBoard, route_board
    from aipcb.route.stitch import stitch_board, stitch_uuids

    report = Report()
    target = out or design.parent
    try:
        result, board_path, board = _build(design, target, report)
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        chosen = (
            tuple(part.strip() for part in layers.split(",") if part.strip())
            if layers
            else None
        )

        def run(manual_copper: bool) -> RoutedBoard:
            return route_board(
                board,
                result.netlist,
                report,
                layers=chosen,
                topologies=topologies,
                congestion=congestion,
                manual_copper=manual_copper,
            )

        # Copper already in the board is either somebody's hand routing, which must
        # be preserved and routed around, or this command's own output from a
        # previous run, which must be replaced rather than duplicated. Routing once
        # while ignoring all of it says which UUIDs *we* would produce, and anything
        # in the board carrying one of those is ours. Only run when there is copper
        # to sort out, which on a first build there is not.
        #
        # Last run's stitching vias go first, and before the router looks at the
        # board at all: they are ours too, and leaving them in would make this run's
        # routing depend on the previous run's stitching, which is the end of
        # byte-stability.
        drop_generated(board, stitch_uuids(result.netlist))
        if list(board.children("segment")) or list(board.children("via")):
            owned = generated_uuids(run(False).connections)
            drop_generated(board, owned)
        routed = run(True)
        count, via_count = attach_copper(
            board, routed.connections, sorted(result.netlist.nets)
        )
        stitched = stitch_board(board, result.netlist, report)
        board_path.write_text(dump(board), encoding="utf-8")
    except SourceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AipcbError as exc:
        typer.echo(exc.report.render(color=sys.stdout.isatty()), err=True)
        raise typer.Exit(1) from exc

    summary = routed.summary()
    layers = ", ".join(str(name) for name in sorted({leg.layer for leg in routed.routed}))
    summary["segments"] = count
    summary["vias"] = via_count
    if stitched.placed or stitched.total_skipped:
        summary["stitching"] = stitched.summary()
    if as_json:
        payload = report.to_dict()
        payload["routing"] = summary
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(
            f"routed {summary['routed']} connections "
            f"({summary['failed']} unrouted) on "
            f"{layers or 'no layers'}, "
            f"{count} track segments and {via_count} vias, "
            f"{summary['length_mm']} mm of copper"
        )
        if stitched.placed or stitched.total_skipped:
            typer.echo(
                f"stitched {len(stitched.placed)} vias "
                f"({stitched.total_skipped} positions skipped)"
            )
        for handed in routed.handed_over():
            typer.echo(
                f"  unrouted ({handed['unrouted']}): {handed['net']} "
                f"{handed['from']} -> {handed['to']}"
            )
        typer.echo(f"wrote {board_path}")
    # Unrouted connections are reported, not fatal: a partly routed board is a
    # useful thing to open in KiCad and finish by hand.
    raise typer.Exit(1 if not report.ok else 0)

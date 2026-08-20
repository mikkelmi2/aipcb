"""The ``aipcb route`` commands."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from aipcb.diagnostics import AipcbError, Report
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
    layer: Annotated[str, typer.Option("--layer", help="Layer to route on.")] = "F.Cu",
    as_json: JsonOpt = False,
) -> None:
    """Build a design and route it, writing tracks into the board."""
    from aipcb.kicad.sexpr import dump
    from aipcb.route.emit import attach_tracks
    from aipcb.route.plan import route_board

    report = Report()
    target = out or design.parent
    try:
        result, board_path, board = _build(design, target, report)
        topologies = tuple(result.netlist.layout.routes) if result.netlist.layout else ()
        routed = route_board(
            board, result.netlist, report, layer=layer, topologies=topologies
        )
        count = attach_tracks(
            board, routed.with_endpoints(), sorted(result.netlist.nets)
        )
        board_path.write_text(dump(board), encoding="utf-8")
    except SourceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AipcbError as exc:
        typer.echo(exc.report.render(color=sys.stdout.isatty()), err=True)
        raise typer.Exit(1) from exc

    summary = routed.summary()
    summary["segments"] = count
    if as_json:
        payload = report.to_dict()
        payload["routing"] = summary
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(
            f"routed {summary['routed']} connections "
            f"({summary['failed']} unrouted), {count} track segments, "
            f"{summary['length_mm']} mm of copper"
        )
        typer.echo(f"wrote {board_path}")
    # Unrouted connections are reported, not fatal: a partly routed board is a
    # useful thing to open in KiCad and finish by hand.
    raise typer.Exit(1 if not report.ok else 0)

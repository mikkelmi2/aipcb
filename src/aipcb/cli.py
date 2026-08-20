"""The ``aipcb`` command-line interface.

Every command follows the same contract, because an agent in a loop should never
have to special-case one of them:

* human-readable text by default, ``--json`` for the machine-readable form;
* exit code 0 on success, 1 when errors were found, 2 when the input could not be
  read at all;
* diagnostics that name a file, a line, and where possible a fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from aipcb.diagnostics import AipcbError, Report
from aipcb.source import SourceError

app = typer.Typer(
    name="aipcb",
    help="Compile a semantic electronics source format into KiCad.",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_UNREADABLE = 2

DesignArg = Annotated[
    Path,
    typer.Argument(
        help="Path to the design file.",
        exists=False,
        dir_okay=False,
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _err(message: str) -> None:
    typer.echo(message, err=True)


def _emit(report: Report, as_json: bool) -> None:
    if as_json:
        typer.echo(report.to_json())
    else:
        typer.echo(report.render(color=sys.stdout.isatty()))


@app.command()
def validate(
    design: DesignArg,
    as_json: JsonOpt = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat warnings as errors.")
    ] = False,
) -> None:
    """Check a design against the schema and the semantic rules."""
    from aipcb.checks.kicad_bindings import check_kicad_bindings
    from aipcb.checks.semantic import run_semantic_checks
    from aipcb.elaborate import elaborate
    from aipcb.loader import load_design

    report = Report()
    try:
        loaded = load_design(design, report=report)
    except SourceError as exc:
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    netlist = elaborate(loaded, report)
    run_semantic_checks(netlist, report)
    check_kicad_bindings(loaded, report)

    if as_json:
        payload = report.to_dict()
        payload["design"] = {
            "name": netlist.name,
            "revision": netlist.revision,
            **netlist.stats(),
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty()))
        if report.ok:
            stats = netlist.stats()
            typer.echo(
                f"{netlist.name} rev {netlist.revision}: "
                f"{stats['components']} components, {stats['nets']} nets, "
                f"{stats['nodes']} connections"
            )

    failed = not report.ok or (strict and report.warnings)
    raise typer.Exit(EXIT_ERRORS if failed else EXIT_OK)


@app.command()
def build(
    design: DesignArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output directory. Defaults to the design's."),
    ] = None,
    as_json: JsonOpt = False,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Regenerate from source, discarding manual edits to an existing board.",
        ),
    ] = False,
) -> None:
    """Compile a design into KiCad files.

    Incremental by default: an existing board is read first, and hand-placed
    footprints, hand-routed copper and hand-drawn zones are carried over.
    """
    from aipcb.compile.build import build_design

    report = Report()
    try:
        result = build_design(design, out_dir=out, report=report, fresh=fresh)
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    if as_json:
        payload = report.to_dict()
        payload["outputs"] = [str(p) for p in result.written]
        payload["design"] = {"name": result.netlist.name, **result.netlist.stats()}
        if result.merge is not None:
            payload["preserved"] = result.merge.to_dict()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        for path in result.written:
            typer.echo(f"wrote {path}")
    raise typer.Exit(EXIT_ERRORS if not report.ok else EXIT_OK)


@app.command()
def export(
    design: DesignArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Where the fabrication files go."),
    ] = None,
    as_json: JsonOpt = False,
    keep_build: Annotated[
        Path | None,
        typer.Option("--build-dir", help="Keep the intermediate KiCad files here."),
    ] = None,
) -> None:
    """Build a design and export Gerbers, drill files, a BOM and a placement file."""
    import tempfile

    from aipcb.compile.build import build_design
    from aipcb.compile.export import export_board

    report = Report()
    target = out or (design.parent / "out")

    def run(build_dir: Path) -> Any:
        result = build_design(design, out_dir=build_dir, report=report)
        board = next((p for p in result.written if p.suffix == ".kicad_pcb"), None)
        sch = next((p for p in result.written if p.suffix == ".kicad_sch"), None)
        if board is None:
            raise AipcbError("no board was produced", report)
        return export_board(board, target, result.netlist, report, schematic=sch)

    try:
        if keep_build is not None:
            keep_build.mkdir(parents=True, exist_ok=True)
            exported = run(keep_build)
        else:
            with tempfile.TemporaryDirectory(prefix="aipcb-export-") as tmp:
                exported = run(Path(tmp))
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    if as_json:
        payload = report.to_dict()
        payload["export"] = {
            "directory": str(exported.directory),
            "steps": exported.steps,
            "files": [str(p) for p in exported.files],
            "by_suffix": exported.by_suffix(),
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(
            f"exported {len(exported.files)} files to {exported.directory} "
            f"({', '.join(exported.steps) or 'nothing'})"
        )
    raise typer.Exit(EXIT_ERRORS if not (report.ok and exported.ok) else EXIT_OK)


@app.command()
def summary(design: DesignArg, as_json: JsonOpt = False) -> None:
    """A one-line-per-block overview of a design.

    Written to be the first thing read about an unfamiliar design, and small
    enough that reading it is never the expensive choice.
    """
    from aipcb.cli_query import load, render_summary
    from aipcb.query import summarise_design

    data = summarise_design(load(design))
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(render_summary(data))


@app.command()
def check(
    design: DesignArg,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Keep the generated KiCad files here."),
    ] = None,
    as_json: JsonOpt = False,
    skip_erc: Annotated[bool, typer.Option("--no-erc", help="Skip the ERC run.")] = False,
    skip_drc: Annotated[bool, typer.Option("--no-drc", help="Skip the DRC run.")] = False,
) -> None:
    """Build a design and run KiCad's ERC and DRC against it.

    Violations are reported against the source that produced them, not against
    coordinates on a sheet.
    """
    from aipcb.checks.loop import check_design

    report = Report()
    try:
        result = check_design(
            design,
            out_dir=out,
            report=report,
            schematic=not skip_erc,
            board=not skip_drc,
        )
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    if as_json:
        payload = report.to_dict()
        payload["summary"] = result.summary()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty()))
        summary = result.summary()
        typer.echo(
            f"{summary['design']} rev {summary['revision']}: "
            f"{summary['components']} components, {summary['nets']} nets  "
            f"[erc {'ran' if result.erc.ran else 'skipped'}, "
            f"drc {'ran' if result.drc.ran else 'skipped'}]"
        )
    raise typer.Exit(EXIT_ERRORS if not report.ok else EXIT_OK)


@app.command()
def parts(
    design: DesignArg,
    as_json: JsonOpt = False,
) -> None:
    """List the parts a design's libraries make available."""
    from aipcb.loader import load_design

    report = Report()
    try:
        loaded = load_design(design, report=report)
    except (SourceError, AipcbError) as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc

    if as_json:
        typer.echo(
            json.dumps(
                {
                    name: part.model_dump(mode="json", exclude_defaults=True)
                    for name, part in sorted(loaded.parts.items())
                },
                indent=2,
            )
        )
        return

    if not loaded.parts:
        typer.echo("no part libraries are loaded; add one under `libraries:`")
        return
    width = max(len(n) for n in loaded.parts)
    for name, part in sorted(loaded.parts.items()):
        typer.echo(f"{name:<{width}}  {part.symbol:<28} {part.description or ''}")


@app.command()
def schema(
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write to this file.")
    ] = None,
) -> None:
    """Emit the JSON Schema for the source format, for editor completion."""
    from aipcb.model.design import Design

    text = json.dumps(Design.model_json_schema(), indent=2)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")


@app.command()
def version() -> None:
    """Print the version of aipcb, and of the KiCad it will drive."""
    from aipcb import __version__
    from aipcb.kicad.cli import kicad_version

    typer.echo(f"aipcb {__version__}")
    found = kicad_version()
    typer.echo(f"kicad-cli {found}" if found else "kicad-cli: not found on PATH")


from aipcb.cli_query import query_app  # noqa: E402

app.add_typer(query_app)


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

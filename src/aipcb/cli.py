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
from typing import Annotated

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


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

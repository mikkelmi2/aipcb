# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
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
    render: Annotated[
        bool,
        typer.Option(
            "--render",
            help="Also plot the schematic to review/ as PDF and SVG, with its "
            "readability measurements.",
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
        result = build_design(
            design, out_dir=out, report=report, fresh=fresh, render=render
        )
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
        if result.review is not None:
            payload["review"] = result.review.to_dict()
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
    dsn: Annotated[
        bool,
        typer.Option(
            "--dsn",
            help="Export a Specctra DSN for an external router instead of "
            "fabrication files. Existing copper is fixed in the file.",
        ),
    ] = False,
    board: Annotated[
        Path | None,
        typer.Option(
            "--board",
            help="With --dsn: the board to export. Defaults to the one beside the "
            "design file.",
        ),
    ] = None,
) -> None:
    """Build a design and export Gerbers, drill files, a BOM and a placement file.

    With `--dsn`, export the board as a Specctra DSN instead, for an external router
    to work on. Everything already routed or poured is marked unmovable in the file,
    and the declared-manual nets with no copper yet are listed as what is left to do.
    aipcb does not run the router: see `docs/external-routers.md` for the three
    commands and the contract.
    """
    import tempfile

    if dsn:
        _export_dsn(design, out, board, as_json)
        return

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


@app.command("sync-placement")
def sync_placement(
    design: DesignArg,
    board: Annotated[
        Path | None,
        typer.Option(
            "--board",
            help="The board to read positions from. Defaults to the one beside the "
            "design file.",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the board's positions into the design file."),
    ] = False,
    assume_yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Accept every change without asking.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Report parts that have been moved in KiCad, and offer to update the source.

    A `fixed:` placement is mechanical law, so `aipcb build` puts a hand-moved part
    back where the source says and says that it did. This is the other direction:
    when the board is right and the YAML is stale, it writes the board's position
    into the YAML -- in place, keeping every comment and every `reason:`.

    Without `--apply` nothing is written; the drift is only listed.
    """
    from aipcb.compile.build import compile_netlist, project_name
    from aipcb.compile.frame import frame_for
    from aipcb.compile.place import component_extents, plan_placement
    from aipcb.compile.sync import apply_drift, find_drift, read_board
    from aipcb.kicad.sexpr import SExprError

    report = Report()
    try:
        netlist = compile_netlist(design, report)
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    board_path = board or (design.parent / f"{project_name(netlist.name)}.kicad_pcb")
    frame = frame_for(netlist)
    if frame is None or not netlist.placement:
        message = (
            "this design declares no `placement:` block, so there is nothing to sync"
        )
        typer.echo(json.dumps({"ok": True, "drift": [], "note": message}, indent=2)
                   if as_json else message)
        raise typer.Exit(EXIT_OK)
    if not board_path.exists():
        _err(f"error: no board at {board_path}; run `aipcb build` first")
        raise typer.Exit(EXIT_UNREADABLE)

    try:
        tree = read_board(board_path)
    except (OSError, UnicodeDecodeError, SExprError) as exc:
        _err(f"error: cannot read {board_path}: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc

    extents, _ = component_extents(netlist)
    placement = plan_placement(netlist, extents=extents, frame=frame)
    generated = {
        refdes: (placed.x, placed.y, placed.rotation)
        for refdes, placed in placement.positions.items()
    }
    drifts = find_drift(netlist, tree, frame, generated)

    written: list[str] = []
    if apply and drifts:
        accepted = [
            d
            for d in drifts
            if assume_yes
            or as_json
            or typer.confirm(f"{d.describe()}\n  write this into the source?", default=True)
        ]
        if accepted:
            text, written = apply_drift(
                design.read_text(encoding="utf-8"), netlist, accepted
            )
            design.write_text(text, encoding="utf-8")
        missed = sorted({d.refdes for d in accepted} - set(written))
        if missed and not as_json:
            # The rewriter edits the `fixed:`/`edge:`/`region:` key in place, and
            # needs to find it on its own line. An entry written as a one-line flow
            # mapping is legal YAML and not something to rewrite blindly.
            _err(
                f"could not update {', '.join(missed)} in {design}: expand the entry "
                "so its key is on its own line, or edit it by hand"
            )

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": True,
                    "board": str(board_path),
                    "drift": [d.to_dict() for d in drifts],
                    "written": written,
                },
                indent=2,
            )
        )
    elif not drifts:
        typer.echo(f"every fixed and constrained part is where {design.name} says")
    else:
        for drift in drifts:
            typer.echo(drift.describe())
        if written:
            typer.echo(f"wrote {', '.join(written)} into {design}")
        elif not apply:
            typer.echo(
                f"\n{len(drifts)} part{'s' if len(drifts) != 1 else ''} moved. "
                "Re-run with --apply to write these positions into the source, or "
                "run `aipcb build` to put them back where the source says."
            )
    raise typer.Exit(EXIT_OK)


def _default_board(design: Path, netlist: Any) -> Path:
    from aipcb.compile.build import project_name

    return design.parent / f"{project_name(netlist.name)}.kicad_pcb"


def _export_dsn(
    design: Path, out: Path | None, board: Path | None, as_json: bool
) -> None:
    """`aipcb export --dsn`: hand a board to an external router, safely."""
    from aipcb.compile.build import compile_netlist, project_name
    from aipcb.kicad.specctra import SpecctraError
    from aipcb.route.bridge import export_for_router

    report = Report()
    try:
        netlist = compile_netlist(design, report)
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    board_path = board or _default_board(design, netlist)
    if not board_path.exists():
        _err(
            f"error: no board at {board_path}. Run `aipcb route all {design}` first: "
            "the DSN has to carry the copper that is already there, or an external "
            "router will re-route it"
        )
        raise typer.Exit(EXIT_UNREADABLE)

    target_dir = out or design.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{project_name(netlist.name)}.dsn"

    try:
        result = export_for_router(board_path, target, netlist, report)
    except (ValueError, SpecctraError) as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_ERRORS) from exc

    if as_json:
        payload = report.to_dict()
        payload["export"] = result.to_dict()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(
            f"wrote {target} ({result.dsn.bytes_written} bytes, "
            f"{result.dsn.protected} pieces of copper fixed)"
        )
        if result.pending:
            typer.echo(f"  to route: {', '.join(result.pending)}")
    raise typer.Exit(EXIT_ERRORS if not report.ok else EXIT_OK)


@app.command(name="import")
def import_cmd(
    design: DesignArg,
    ses: Annotated[
        Path,
        typer.Option(
            "--ses",
            help="The Specctra session file an external router produced.",
        ),
    ],
    board: Annotated[
        Path | None,
        typer.Option(
            "--board",
            help="The board to import into. Defaults to the one beside the design.",
        ),
    ] = None,
    as_json: JsonOpt = False,
    skip_check: Annotated[
        bool,
        typer.Option("--no-check", help="Import without running ERC and DRC after."),
    ] = False,
) -> None:
    """Import an external router's session file, then check what arrived.

    Imported copper is *manual* copper: aipcb preserves it on every later build,
    routes around it, and checks it exactly as it checks its own. What it does not do
    is trust it -- the widths and via sizes are compared against the net classes the
    source declares, and any difference is reported rather than accepted.
    """
    from aipcb.checks.loop import check_design
    from aipcb.compile.build import compile_netlist
    from aipcb.kicad.specctra import SpecctraError
    from aipcb.route.bridge import import_session

    report = Report()
    try:
        netlist = compile_netlist(design, report)
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _emit(exc.report, as_json)
        raise typer.Exit(EXIT_ERRORS) from exc

    board_path = board or _default_board(design, netlist)
    for path, what in ((board_path, "board"), (ses, "session file")):
        if not path.exists():
            _err(f"error: no {what} at {path}")
            raise typer.Exit(EXIT_UNREADABLE)

    try:
        result = import_session(board_path, ses, netlist, report)
    except (ValueError, SpecctraError) as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_ERRORS) from exc

    checked = None
    if not skip_check:
        # `board_file` is what makes this check mean anything. `check_design` builds
        # fresh on purpose (M13.5), and a fresh build of this design has no copper on
        # it at all -- so without pointing it at the board we just wrote, DRC would
        # report zero errors on an empty board and call the import verified.
        try:
            checked = check_design(
                design, report=report, route=False, board_file=board_path
            )
        except AipcbError as exc:
            _emit(exc.report, as_json)
            raise typer.Exit(EXIT_ERRORS) from exc

    if as_json:
        payload = report.to_dict()
        payload["import"] = result.to_dict()
        if checked is not None:
            payload["summary"] = checked.summary()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(report.render(color=sys.stdout.isatty(), summary=bool(report)))
        typer.echo(
            f"imported {result.ses.tracks_added} tracks and "
            f"{result.ses.vias_added} vias into {board_path}"
        )
        for state, count in result.states.counts().items():
            typer.echo(f"  {state}: {count}")
        if checked is not None:
            typer.echo(
                f"  drc: {'ran' if checked.drc.ran else 'skipped'} "
                f"({checked.drc.counts.get('errors', 0)} errors)"
            )
    raise typer.Exit(EXIT_ERRORS if not report.ok else EXIT_OK)


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
    skip_route: Annotated[
        bool,
        typer.Option(
            "--no-route", help="Check the board as built, without routing it first."
        ),
    ] = False,
) -> None:
    """Build a design, route it, and run KiCad's ERC and DRC against the result.

    Violations are reported against the source that produced them, not against
    coordinates on a sheet.

    Routing runs first, because a DRC pass over a board with no copper on it has
    checked almost nothing. Connections the router will not deliver legally are
    handed over instead: `--json` lists them under `summary.routing.handed_over`,
    with the corridor that ran out of room and the nets contesting it.
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
            route=not skip_route,
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
        if result.stitching is not None and result.stitching.placed:
            typer.echo(
                f"  stitching: {len(result.stitching.placed)} vias placed, "
                f"{result.stitching.total_skipped} positions skipped"
            )
        if result.fill is not None:
            typer.echo(
                f"  zones: {result.fill.filled}/{result.fill.zones} filled by "
                f"KiCad {result.fill.kicad_version}, {result.fill.islands} islands"
            )
        for handed in result.handed_over:
            typer.echo(
                f"  unrouted ({handed['unrouted']}): {handed['net']} "
                f"{handed['from']} -> {handed['to']}"
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
def bench(
    examples: Annotated[
        str | None,
        typer.Option(
            "--examples",
            help="Comma-separated example names. Defaults to every bundled example.",
        ),
    ] = None,
    smoke: Annotated[
        bool,
        typer.Option(
            "--smoke",
            help="Run only the small CI subset, for a fast regression check.",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Where to write the results file. Defaults to "
            "bench/results/<commit>.json.",
        ),
    ] = None,
    no_write: Annotated[
        bool, typer.Option("--no-write", help="Measure and print, write nothing.")
    ] = False,
    baseline: Annotated[
        Path | None,
        typer.Option(
            "--compare",
            help="A previous results file to diff this run against. Exits 1 on a "
            "regression.",
        ),
    ] = None,
    runtime_threshold: Annotated[
        float | None,
        typer.Option(
            "--runtime-threshold",
            help="How much slower than the baseline counts as a regression, in "
            "percent. Set this high when the baseline was measured elsewhere.",
        ),
    ] = None,
    length_threshold: Annotated[
        float | None,
        typer.Option(
            "--length-threshold",
            help="How much more copper counts as a regression, in percent.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Route every example and record what it cost and what it was worth.

    The benchmark the toporouter never had. Runtime -- not correctness -- is what
    made that router unusable, and it is one of the three conditions autorouting
    has to meet to leave beta. `--compare` diffs against a committed baseline so a
    change to the router has to show its numbers.
    """
    from aipcb import bench as harness

    thresholds = {
        "runtime_threshold": harness.DEFAULT_RUNTIME_THRESHOLD
        if runtime_threshold is None
        else runtime_threshold,
        "length_threshold": harness.DEFAULT_LENGTH_THRESHOLD
        if length_threshold is None
        else length_threshold,
    }
    root = harness.repository_root()
    names: tuple[str, ...] | None = None
    if smoke:
        names = harness.SMOKE_EXAMPLES
    if examples:
        names = tuple(part.strip() for part in examples.split(",") if part.strip())
    try:
        designs = harness.resolve(names, root)
    except KeyError as exc:
        _err(f"error: {exc.args[0]}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    if not designs:
        _err("error: no example designs found to benchmark")
        raise typer.Exit(EXIT_UNREADABLE)

    # The router's own diagnostics are not this command's output. A benchmark that
    # printed every board's notes would bury the table it exists to produce, and the
    # findings that matter -- crossings, failed connections -- are columns in it.
    # A design that will not *build*, though, is not a measurement at all.
    try:
        result = harness.bench_examples(designs, report=Report())
    except SourceError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_UNREADABLE) from exc
    except AipcbError as exc:
        _err(exc.report.render(color=sys.stdout.isatty()))
        raise typer.Exit(EXIT_ERRORS) from exc

    written: Path | None = None
    if not no_write:
        written = out or harness.default_output(result, root)
        harness.write(result, written)

    outcome = None
    if baseline is not None:
        try:
            previous = harness.load(baseline)
        except (OSError, ValueError) as exc:
            _err(f"error: cannot read the baseline {baseline}: {exc}")
            raise typer.Exit(EXIT_UNREADABLE) from exc
        outcome = harness.compare(
            previous, result, expect_all=names is None, **thresholds
        )

    if as_json:
        payload: dict[str, Any] = result.to_dict()
        if written is not None:
            payload["written"] = str(written)
        if outcome is not None:
            payload["comparison"] = outcome.to_dict()
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(harness.render_table(result))
        typer.echo("")
        typer.echo(harness.render_stages(result))
        if written is not None:
            typer.echo(f"\nwrote {written}")
        if outcome is not None:
            typer.echo(f"\nagainst {baseline}:")
            typer.echo(harness.render_comparison(outcome))

    raise typer.Exit(EXIT_ERRORS if outcome is not None and not outcome.ok else EXIT_OK)


@app.command()
def version() -> None:
    """Print the version of aipcb, and of the KiCad it will drive."""
    from aipcb import __version__
    from aipcb.kicad.cli import kicad_version

    typer.echo(f"aipcb {__version__}")
    found = kicad_version()
    typer.echo(f"kicad-cli {found}" if found else "kicad-cli: not found on PATH")


from aipcb.cli_query import query_app  # noqa: E402
from aipcb.cli_route import route_app  # noqa: E402
from aipcb.cli_simulate import register as register_simulate  # noqa: E402

app.add_typer(query_app)
app.add_typer(route_app)
register_simulate(app)


def main() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

"""The ``aipcb query`` and ``aipcb summary`` commands.

Rendering lives here, apart from the queries themselves, so text and JSON are two
views of one structure rather than two implementations that can drift.

The text form is written to be read by a model as much as by a person: dense,
aligned, one fact per line, no decoration that carries no information.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from aipcb.diagnostics import AipcbError, Report
from aipcb.netlist import Netlist
from aipcb.source import SourceError

query_app = typer.Typer(
    name="query",
    help="Read part of a design without loading all of it.",
    no_args_is_help=True,
    add_completion=False,
)

DesignArg = Annotated[Path, typer.Argument(help="Path to the design file.", dir_okay=False)]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def load(design: Path) -> Netlist:
    """Elaborate a design, or exit with the same codes every other command uses."""
    from aipcb.compile.build import compile_netlist

    try:
        return compile_netlist(design, Report())
    except SourceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except AipcbError as exc:
        typer.echo(exc.report.render(color=sys.stdout.isatty()), err=True)
        raise typer.Exit(1) from exc


def _emit(data: Any, as_json: bool, render: Any) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(render(data))


def _missing(kind: str, name: str, available: list[str]) -> None:
    import difflib

    near = difflib.get_close_matches(name, available, n=3, cutoff=0.4)
    message = f"error: no {kind} named {name!r}"
    if near:
        message += f"\n  did you mean: {', '.join(near)}?"
    elif available:
        message += f"\n  available: {', '.join(available[:20])}"
    typer.echo(message, err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_summary(data: dict[str, Any]) -> str:
    lines = [f"{data['design']} rev {data['revision']}"]
    if data.get("description"):
        lines.append(f"  {' '.join(str(data['description']).split())}")
    totals = data["totals"]
    lines.append(
        f"  {totals['components']} components, {totals['nets']} nets, "
        f"{totals['nodes']} connections, {totals['constraints']} constraints"
    )
    classes = ", ".join(f"{k} x{v}" for k, v in data["net_classes"].items())
    lines.append(f"  net classes: {classes}")

    lines.append("")
    lines.append("blocks:")
    width = max((len(b["block"]) for b in data["blocks"]), default=0)
    for block in data["blocks"]:
        roles = f"  [{', '.join(block['roles'])}]" if block["roles"] else ""
        lines.append(
            f"  {block['block']:<{width}}  {block['components']:>2} parts  "
            f"{block['refdes']}{roles}"
        )

    if data["constraints"]:
        lines.append("")
        lines.append("constraints:")
        for constraint in data["constraints"]:
            members = ", ".join(constraint["members"])
            lines.append(f"  {constraint['kind']}: {members}")
            if constraint.get("reason"):
                lines.append(f"    {' '.join(str(constraint['reason']).split())}")
    return "\n".join(lines)


def render_module(data: dict[str, Any]) -> str:
    lines = [f"module {data['module']}"]
    lines.append("  components:")
    for component in data["components"]:
        lines.append(f"    {_component_line(component)}")
    if data["ports"]:
        lines.append("  ports (nets crossing the boundary):")
        for port in data["ports"]:
            lines.append(
                f"    {port['net']} [{port['class']}]  "
                f"inside {', '.join(port['inside'])}  ->  {', '.join(port['outside'])}"
            )
    if data["internal_nets"]:
        lines.append("  internal nets:")
        for net in data["internal_nets"]:
            lines.append(f"    {net['net']} [{net['class']}]  {', '.join(net['inside'])}")
    if data["neighbours"]:
        lines.append("  neighbours:")
        for component in data["neighbours"]:
            lines.append(f"    {_component_line(component)}")
    return "\n".join(lines)


def _component_line(component: dict[str, Any]) -> str:
    bits = [f"{component['refdes']:<5} {component['part']}"]
    if component.get("role"):
        bits.append(f"role={component['role']}")
    if component.get("for"):
        bits.append(f"for={component['for']}")
    if component.get("dnp"):
        bits.append("DNP")
    return "  ".join(bits)


def render_component(data: dict[str, Any]) -> str:
    lines = [f"{data['refdes']}  {data['part']} ({data['value']})"]
    lines.append(f"  path: {data['path']}")
    if data.get("role"):
        lines.append(f"  role: {data['role']}" + (f" for {data['for']}" if data.get("for") else ""))
    if data.get("reason"):
        lines.append(f"  reason: {' '.join(str(data['reason']).split())}")
    if data.get("symbol"):
        lines.append(f"  symbol: {data['symbol']}")
        lines.append(f"  footprint: {data['footprint']}")
    lines.append("  connections:")
    for connection in data["connections"]:
        target = ", ".join(connection["to"]) or "(nothing else)"
        lines.append(
            f"    pin {connection['pin']:<4} {connection['name']:<12} "
            f"{connection['net']} [{connection['class']}]  ->  {target}"
        )
    if data["served_by"]:
        lines.append(f"  served by: {', '.join(data['served_by'])}")
    return "\n".join(lines)


def render_net(data: dict[str, Any]) -> str:
    lines = [f"{data['net']}  [{data['class']}]  {data['degree']} connections"]
    for key in ("voltage", "max_current_a", "impedance_ohm", "diff_pair"):
        if key in data:
            lines.append(f"  {key}: {data[key]}")
    if data.get("description"):
        lines.append(f"  {' '.join(str(data['description']).split())}")
    if data.get("reason"):
        lines.append(f"  reason: {' '.join(str(data['reason']).split())}")
    lines.append(f"  nodes: {', '.join(data['nodes'])}")
    if data.get("rules"):
        rules = ", ".join(f"{k}={v}" for k, v in sorted(data["rules"].items()))
        lines.append(f"  rules: {rules}")
    return "\n".join(lines)


def render_net_class(data: dict[str, Any]) -> str:
    lines = [f"net class {data['class']}  ({data['count']} nets)"]
    if data.get("rules"):
        for key, value in sorted(data["rules"].items()):
            lines.append(f"  {key}: {value}")
    lines.append("  nets:")
    for net in data["nets"]:
        extra = ""
        if "impedance_ohm" in net:
            extra += f"  {net['impedance_ohm']}ohm"
        if "diff_pair" in net:
            extra += f"  pair={net['diff_pair']}"
        lines.append(
            f"    {net['net']:<12} {net['degree']} nodes{extra}  "
            f"{', '.join(net['nodes'])}"
        )
    return "\n".join(lines)


def render_role(data: dict[str, Any]) -> str:
    lines = [f"role {data['role']}  ({data['count']} components)"]
    for component in data["components"]:
        lines.append(f"  {_component_line(component)}")
        if component.get("reason"):
            lines.append(f"      {' '.join(str(component['reason']).split())}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@query_app.command("module")
def query_module(design: DesignArg, name: str, as_json: JsonOpt = False) -> None:
    """One module instance, with the components on the other side of its ports."""
    from aipcb.query import describe_module, list_modules

    netlist = load(design)
    try:
        data = describe_module(netlist, name)
    except KeyError:
        _missing("module instance", name, list_modules(netlist))
        return
    _emit(data, as_json, render_module)


@query_app.command("component")
def query_component(design: DesignArg, refdes: str, as_json: JsonOpt = False) -> None:
    """One component: what it is, why it is there, and what it touches."""
    from aipcb.query import describe_component

    netlist = load(design)
    try:
        data = describe_component(netlist, refdes)
    except KeyError:
        _missing("component", refdes, sorted(netlist.components))
        return
    _emit(data, as_json, render_component)


@query_app.command("net")
def query_net(design: DesignArg, name: str, as_json: JsonOpt = False) -> None:
    """One net, with its electrical attributes and routing rules."""
    from aipcb.query import describe_net

    netlist = load(design)
    try:
        data = describe_net(netlist, name)
    except KeyError:
        _missing("net", name, sorted(netlist.nets))
        return
    _emit(data, as_json, render_net)


@query_app.command("net-class")
def query_net_class(design: DesignArg, name: str, as_json: JsonOpt = False) -> None:
    """Every net in a class, with the class's routing rules."""
    from aipcb.query import nets_of_class

    netlist = load(design)
    data = nets_of_class(netlist, name)
    if data["count"] == 0:
        _missing("net class", name, sorted({n.net_class for n in netlist.nets.values()}))
        return
    _emit(data, as_json, render_net_class)


@query_app.command("role")
def query_role(design: DesignArg, name: str, as_json: JsonOpt = False) -> None:
    """Every component with a given role."""
    from aipcb.query import components_by_role

    netlist = load(design)
    data = components_by_role(netlist, name)
    if data["count"] == 0:
        _missing("role", name, sorted({c.role for c in netlist.components.values() if c.role}))
        return
    _emit(data, as_json, render_role)

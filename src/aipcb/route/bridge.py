# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The external-router bridge: pipes on each side, and judgement in the middle.

ADR 0006 rejected *integrating* an external router, and that decision stands: the
topological router is the thing this project is for, and wiring Freerouting into the
pipeline would mean owning its output without owning its behaviour. What M14e adds
is the position that was missing -- a **documented bridge**, which is a different
thing from an integration:

* ``aipcb`` writes a DSN with everything it has already routed marked unmovable;
* an *agent* runs whatever router it likes, as an explicit step it can see;
* ``aipcb`` reads the session back, checks it against the source, and says what it
  found.

``aipcb`` never invokes the router, parses its logs, or promises anything about its
output. What it promises is the two ends: a DSN that cannot destroy existing work,
and an import that reports drift instead of swallowing it.

The one rule with teeth is at the top of :func:`export_for_router`. A
controlled-impedance class carries a derived width, a gap, a coupling budget and a
reference plane (M11); an external router knows about none of them and will happily
return a pair that is neither coupled nor 85 ohm. Sending one is not forbidden --
somebody may have a reason -- but it is never done quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aipcb.diagnostics import Report
from aipcb.kicad.sexpr import SExprError, SNode, dump, parse, sym
from aipcb.kicad.specctra import DsnResult, SesResult, export_dsn, import_ses
from aipcb.netlist import Netlist
from aipcb.route.manual import RoutingStates, routing_states

__all__ = [
    "COPPER_ITEMS",
    "BridgeExport",
    "BridgeImport",
    "Drift",
    "SpliceStats",
    "export_for_router",
    "import_session",
    "splice_session_copper",
    "verify_against_source",
]

#: The board items a session file can bring in. Zones are not among them: an
#: external router does not pour, and one that claimed to would be overwriting M10's.
COPPER_ITEMS = ("segment", "arc", "via")

#: How far a width or a via size may differ from the class before it is a finding.
#: One micrometre: the units in a session file are integers of 0.1 um, so anything
#: bigger than this is a real difference rather than a rounding artefact.
TOLERANCE_MM = 0.001


@dataclass(slots=True)
class BridgeExport:
    """What ``aipcb export --dsn`` produced."""

    dsn: DsnResult
    states: RoutingStates
    pending: list[str] = field(default_factory=list)
    controlled_pending: list[str] = field(default_factory=list)
    """Declared-manual, still unrouted, and on a controlled-impedance class."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dsn": self.dsn.to_dict(),
            "pending": list(self.pending),
            "controlled_impedance_pending": list(self.controlled_pending),
            "nets": self.states.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Drift:
    """One way the session disagrees with the source."""

    net: str
    kind: str
    expected: float
    found: float

    def describe(self) -> str:
        return (
            f"{self.net}: {self.kind} came back as {self.found:.4f} mm, but the net "
            f"class says {self.expected:.4f} mm"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "net": self.net,
            "kind": self.kind,
            "expected_mm": round(self.expected, 4),
            "found_mm": round(self.found, 4),
        }


@dataclass(slots=True)
class SpliceStats:
    """What was taken out of an imported board, and what was left behind."""

    taken: dict[str, int] = field(default_factory=dict)
    """Per net, how many pieces of copper were spliced in."""
    ignored: dict[str, int] = field(default_factory=dict)
    """Per net, copper the session carried for nets it was told not to touch."""
    protected_kept: int = 0
    """Pieces of the original board's copper the splice preserved."""

    @property
    def total_taken(self) -> int:
        return sum(self.taken.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "taken": dict(sorted(self.taken.items())),
            "ignored": dict(sorted(self.ignored.items())),
            "protected_kept": self.protected_kept,
        }


@dataclass(slots=True)
class BridgeImport:
    """What ``aipcb import --ses`` produced."""

    ses: SesResult
    states: RoutingStates
    drift: list[Drift] = field(default_factory=list)
    still_pending: list[str] = field(default_factory=list)
    splice: SpliceStats = field(default_factory=SpliceStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.ses.to_dict(),
            "drift": [d.to_dict() for d in self.drift],
            "still_pending": list(self.still_pending),
            "splice": self.splice.to_dict(),
            "nets": self.states.to_dict(),
        }


def _read(board_path: Path) -> SNode:
    try:
        return parse(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SExprError) as exc:
        raise ValueError(f"cannot read {board_path}: {exc}") from exc


def export_for_router(
    board_path: Path, target: Path, netlist: Netlist, report: Report
) -> BridgeExport:
    """Write a DSN for an external router, and say what it is being asked to do.

    Everything already on the board is fixed in the file, so an external router can
    only add. What it is being asked to route is whatever is left: the declared-manual
    nets that have no copper yet, listed by name, because "route the rest" is not a
    thing a DSN can say and an agent has to know which nets to look for on the way
    back.
    """
    board = _read(board_path)
    states = routing_states(board, netlist)
    pending = [n.net for n in states.pending]
    controlled = [n.net for n in states.pending if n.controlled_impedance]

    if controlled:
        report.warning(
            "controlled-impedance-to-external-router",
            f"{', '.join(controlled)} "
            f"{'are' if len(controlled) != 1 else 'is'} on a controlled-impedance "
            "net class and about to be handed to an external router",
            hint="an external router knows nothing about coupling, skew, the "
            "reference plane or the derived pair geometry, and aipcb cannot check "
            "what it did not decide. Route these with `aipcb route all` or by hand, "
            "and send the rest. See docs/external-routers.md",
        )

    result = export_dsn(board_path, target)
    if not pending:
        report.info(
            "dsn-nothing-pending",
            "every net on this board already has copper, so the DSN asks the "
            "external router for nothing",
            hint="declare the nets you want routed externally with `routing: manual` "
            "and run `aipcb route all` first",
        )
    else:
        report.info(
            "dsn-exported",
            f"{target.name}: {result.protected} pieces of existing copper are fixed, "
            f"{len(pending)} net{'s' if len(pending) != 1 else ''} left to route "
            f"({', '.join(pending)})",
        )
    return BridgeExport(
        dsn=result, states=states, pending=pending, controlled_pending=controlled
    )


def verify_against_source(
    session: SesResult, netlist: Netlist, report: Report
) -> list[Drift]:
    """Compare what came back with what the source asked for.

    SES import is the delicate link in the chain: the format carries geometry and
    net names, and everything else -- which net class a track belongs to, what width
    that class specifies, which via type it should use -- is reconstructed by the
    importer from the names. Reconstruction can be wrong, and a track that is 0.25 mm
    wide on a net whose class says 0.4 mm is a current-carrying mistake that nothing
    downstream would question.

    So it is checked here, per net, against the class the *source* declares -- and
    reported. Never corrected: silently widening somebody else's track would make
    this bridge a router, which is the thing ADR 0006 declined to be.
    """
    from aipcb.route.geometry import class_for

    drift: list[Drift] = []
    for net_name in sorted(session.nets_touched):
        if net_name not in netlist.nets:
            continue
        rules = class_for(netlist, net_name)
        for width in session.widths_mm.get(net_name, ()):
            if abs(width - rules.trace_width_mm) > TOLERANCE_MM:
                drift.append(
                    Drift(net_name, "track width", rules.trace_width_mm, width)
                )
        for diameter, hole in session.via_sizes_mm.get(net_name, ()):
            if abs(diameter - rules.via_diameter_mm) > TOLERANCE_MM:
                drift.append(
                    Drift(net_name, "via diameter", rules.via_diameter_mm, diameter)
                )
            if abs(hole - rules.via_drill_mm) > TOLERANCE_MM:
                drift.append(Drift(net_name, "via drill", rules.via_drill_mm, hole))

    if drift:
        report.warning(
            "session-geometry-drift",
            f"the imported session disagrees with the source about "
            f"{len(drift)} piece{'s' if len(drift) != 1 else ''} of geometry",
            hint="external copper is manual copper: aipcb reports the difference and "
            "changes nothing. Fix it in KiCad, or set the external router's rules to "
            "match the net classes and re-run it",
        )
        for one in drift:
            report.info("session-geometry-drift-detail", one.describe())
    return drift


def _net_names(board: SNode) -> dict[str, str]:
    """A board's net-code to net-name table."""
    return {
        code: name
        for node in board.children("net")
        if (code := node.value(0)) is not None and (name := node.value(1)) is not None
    }


def splice_session_copper(
    original: SNode, imported: SNode, wanted: set[str]
) -> tuple[SNode, SpliceStats]:
    """Take only the copper the router was asked for, and keep everything else.

    This function exists because of something measured rather than assumed.
    ``pcbnew.ImportSpecctraSES`` does not *add* a session's routing to a board -- it
    **replaces** the board's routing with it. Importing a session that routed four
    ISP signals into ``examples/mcu-4layer`` removed 97 tracks and 52 stitching vias
    that were already there, and the board came back with four nets routed and seven
    unrouted. Nothing in the process said so: the file parsed, the import returned
    success, and DRC found no errors, because copper that is gone violates no rule.

    Marking the existing copper ``(type fix)`` in the DSN is what stops the *router*
    moving it, and it does; this is the other half, and it is about the *importer*.
    So the session is imported into a copy, and only the copper on the nets that were
    actually pending is lifted out of that copy and added to the real board. Anything
    the session carried for a net it was told not to touch is counted and left where
    it was, because an external router returning copper for fixed nets is a thing
    worth being told about rather than a thing to merge.

    Net *codes* are not comparable between the two boards -- the importer renumbers --
    so everything is matched by name through each board's own table.
    """
    from_names = _net_names(imported)
    to_codes = {name: code for code, name in _net_names(original).items()}
    stats = SpliceStats(
        protected_kept=sum(
            len(list(original.children(kind))) for kind in COPPER_ITEMS
        )
    )

    for kind in COPPER_ITEMS:
        for item in imported.children(kind):
            code = item.get("net")
            name = from_names.get(code) if code is not None else None
            if name is None:
                continue
            if name not in wanted:
                stats.ignored[name] = stats.ignored.get(name, 0) + 1
                continue
            target_code = to_codes.get(name)
            if target_code is None:
                continue
            item.replace("net", SNode("net").add(sym(target_code)))
            original.add(item)
            stats.taken[name] = stats.taken.get(name, 0) + 1
    return original, stats


def import_session(
    board_path: Path,
    session_path: Path,
    netlist: Netlist,
    report: Report,
    *,
    target: Path | None = None,
) -> BridgeImport:
    """Read a session file into the board, then say what actually arrived.

    The session is imported into a scratch copy and the copper for the nets that
    were pending is spliced into the real board -- see :func:`splice_session_copper`
    for why that indirection is not paranoia. The result is written where the rest of
    the toolchain expects to find it, and the imported copper is *manual* copper in
    M6's sense: unrecognised UUIDs, preserved on every later rebuild, routed around
    rather than through.
    """
    import tempfile

    destination = target or board_path
    original = _read(board_path)
    before = routing_states(original, netlist)
    wanted = {n.net for n in before.pending}

    with tempfile.TemporaryDirectory(prefix="aipcb-ses-") as tmp:
        staged = Path(tmp) / board_path.name
        result = import_ses(board_path, session_path, staged)
        imported = _read(staged)

    merged, splice = splice_session_copper(original, imported, wanted)
    destination.write_text(dump(merged), encoding="utf-8")

    states = routing_states(merged, netlist)
    drift = verify_against_source(result, netlist, report)
    still_pending = [n.net for n in states.pending]

    report.info(
        "ses-imported",
        f"{session_path.name}: {splice.total_taken} pieces of copper on "
        f"{len(splice.taken)} net{'s' if len(splice.taken) != 1 else ''} spliced in, "
        f"{splice.protected_kept} already on the board kept",
        hint="imported copper is manual copper: it is preserved by every later "
        "build and routed around, never through",
    )
    if splice.ignored:
        report.warning(
            "session-touched-fixed-nets",
            "the session carried copper for nets the DSN fixed, and it was ignored: "
            + ", ".join(
                f"{net} ({count})" for net, count in sorted(splice.ignored.items())
            ),
            hint="the external router either ignored the `fix` type or re-drew what "
            "was already there. The board keeps aipcb's copper; nothing was lost",
        )
    if still_pending:
        report.warning(
            "routing-manual-pending",
            f"{len(still_pending)} declared-manual net"
            f"{'s' if len(still_pending) != 1 else ''} still have no copper: "
            f"{', '.join(still_pending)}",
            hint="the external router did not route them; check its own report for "
            "why, or route them by hand",
        )
    return BridgeImport(
        ses=result,
        states=states,
        drift=drift,
        still_pending=still_pending,
        splice=splice,
    )

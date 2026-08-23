# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Running KiCad's ERC and DRC, and re-expressing the results against the source.

`kicad-cli` reports violations against a schematic or a board: a symbol at these
coordinates, a pad with this UUID. That is the wrong frame for an agent editing
YAML. This module runs the checks and rewrites every violation as an ordinary
:class:`~aipcb.diagnostics.Diagnostic` pointing at the line of source that owns the
offending element, so KiCad's findings arrive in exactly the same shape as our own.

The rewrite is exact rather than heuristic. Every item in a report carries a UUID,
and every UUID we emit is a hash of a source path, so the mapping is a dictionary
lookup — see :mod:`aipcb.checks.mapping`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aipcb.checks.mapping import SourceRef, UuidIndex
from aipcb.diagnostics import Diagnostic, Report, Severity
from aipcb.kicad.cli import KicadCliMissing, KicadRun, run_kicad

__all__ = [
    "SEVERITY_MAP",
    "CheckOutcome",
    "parse_drc_report",
    "parse_erc_report",
    "run_drc",
    "run_erc",
]

#: KiCad's severities, mapped onto ours. ``exclusion`` means the user silenced it
#: in KiCad, so it is reported as a note rather than dropped: an exclusion made in
#: the GUI is invisible from the source, and silently honouring it would let a real
#: problem disappear from the agent's view.
SEVERITY_MAP = {
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "exclusion": Severity.INFO,
    "info": Severity.INFO,
    "ignore": Severity.INFO,
}

#: Violation kinds that simply mean "not routed yet". `aipcb build` stops at
#: footprints and an outline, so they are expected on a board nobody has routed and
#: are reported as notes rather than drowning the real findings.
UNROUTED_TYPES = frozenset({"unconnected_items"})

_HINTS = {
    "unconnected_items": "this net has no copper joining its pads yet; run "
    "`aipcb route all` to lay it",
    "clearance": "widen the clearance for this net class under `net_classes:`, or "
    "move the parts apart with a `keep_apart` constraint",
    "copper_edge_clearance": "the part is too close to the board edge; enlarge "
    "`layout.outline` or increase `layout.placement.margin_mm`",
    # KiCad's rule is `courtyards_overlap`, plural. It was spelt singular here from
    # M4 until M13.5, so the hint had never once been attached to the violation it
    # was written for -- found by enumerating KiCad 9.0.8's rule names rather than
    # by anybody hitting it.
    "courtyards_overlap": "two parts overlap; give the placer more room in "
    "`layout.outline`",
    "track_width": "the track is narrower than the net class allows; check "
    "`trace_width_mm` for this class",
    "power_pin_not_driven": "no pin on this net sources power; give the part that "
    "feeds it a `power_out` pin, or check the net's class",
    "pin_not_connected": "connect the pin, or give it pin type `no_connect` in the "
    "part definition so the intent is explicit",
    "lib_footprint_mismatch": "the footprint in the board differs from the one the "
    "part declares; rebuild rather than editing the board's footprint",
    "footprint_symbol_mismatch": "the board and schematic disagree about this "
    "part's footprint; rebuild from source",
    "tracks_crossing": "two nets' copper occupies the same place, which is a short; "
    "this is a router defect rather than a design one -- report the board",
    "shorting_items": "two nets are joined by copper; if the router laid it, report "
    "the board, and if it is a net-tie, say so in the source",
    "missing_courtyard": "this footprint declares no courtyard, so KiCad cannot "
    "check it for overlap; the fix belongs in the footprint library",
    "pth_inside_courtyard": "a through-hole lands inside a part's courtyard; move "
    "the parts apart with a `keep_apart` constraint",
    "npth_inside_courtyard": "a non-plated hole lands inside a part's courtyard; "
    "move the mounting hole, or the part",
}


@dataclass(slots=True)
class CheckOutcome:
    """What one check run produced."""

    ran: bool = False
    """False when the tool could not be run at all."""
    source: str = ""
    diagnostics: list[Diagnostic] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    command: str = ""

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for diagnostic in self.diagnostics:
            out[diagnostic.severity.value] = out.get(diagnostic.severity.value, 0) + 1
        return out


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def _load(report_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return None


def _failed(run: KicadRun, kind: str, report: Report) -> CheckOutcome:
    detail = (run.stderr or run.stdout).strip() or "no output"
    report.error(
        f"{kind}-failed",
        f"kicad-cli could not run {kind.upper()}: {detail.splitlines()[0]}",
        hint=f"command was: {run.command}",
    )
    return CheckOutcome(ran=False, command=run.command)


def run_erc(schematic: Path, index: UuidIndex, report: Report, *, work: Path) -> CheckOutcome:
    """Run KiCad's electrical rules check and map its findings onto the source."""
    output = work / "erc.json"
    try:
        run = run_kicad(
            "sch", "erc", "--format", "json", "--severity-all",
            "-o", str(output), str(schematic),
        )
    except KicadCliMissing as exc:
        report.info("kicad-cli-missing", str(exc).splitlines()[0], hint=str(exc))
        return CheckOutcome(ran=False)

    if run.returncode != 0:
        return _failed(run, "erc", report)
    payload = _load(output)
    if payload is None:
        return _failed(run, "erc", report)

    diagnostics = parse_erc_report(payload, index)
    report.extend(diagnostics)
    return CheckOutcome(True, str(schematic), diagnostics, payload, run.command)


def run_drc(
    board: Path,
    index: UuidIndex,
    report: Report,
    *,
    work: Path,
    schematic_parity: bool = True,
) -> CheckOutcome:
    """Run KiCad's design rules check and map its findings onto the source."""
    output = work / "drc.json"
    args = [
        "pcb", "drc", "--format", "json", "--severity-all",
        "-o", str(output), str(board),
    ]
    if schematic_parity:
        args.insert(2, "--schematic-parity")
    try:
        run = run_kicad(*args)
    except KicadCliMissing as exc:
        report.info("kicad-cli-missing", str(exc).splitlines()[0], hint=str(exc))
        return CheckOutcome(ran=False)

    if run.returncode != 0:
        return _failed(run, "drc", report)
    payload = _load(output)
    if payload is None:
        return _failed(run, "drc", report)

    diagnostics = parse_drc_report(payload, index)
    report.extend(diagnostics)
    return CheckOutcome(True, str(board), diagnostics, payload, run.command)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_erc_report(payload: dict[str, Any], index: UuidIndex) -> list[Diagnostic]:
    """Turn an ERC report into source-referenced diagnostics."""
    out: list[Diagnostic] = []
    for sheet in payload.get("sheets", []):
        for violation in sheet.get("violations", []):
            out.append(_violation(violation, index, origin="erc"))
    return out


def parse_drc_report(payload: dict[str, Any], index: UuidIndex) -> list[Diagnostic]:
    """Turn a DRC report into source-referenced diagnostics.

    KiCad splits its findings into three lists. They are kept distinct in the
    ``context`` so a caller can tell a clearance error from an unrouted net from a
    schematic/board disagreement.
    """
    out: list[Diagnostic] = []
    for key, origin in (
        ("violations", "drc"),
        ("unconnected_items", "drc-unconnected"),
        ("schematic_parity", "drc-parity"),
    ):
        for violation in payload.get(key, []) or []:
            out.append(_violation(violation, index, origin=origin))
    return out


def _violation(violation: dict[str, Any], index: UuidIndex, *, origin: str) -> Diagnostic:
    kind = str(violation.get("type", "unknown"))
    severity = SEVERITY_MAP.get(str(violation.get("severity", "error")), Severity.ERROR)
    if kind in UNROUTED_TYPES:
        severity = Severity.INFO

    items = violation.get("items") or []
    refs = [ref for item in items if (ref := _resolve(item, index)) is not None]
    primary = refs[0] if refs else None

    message = str(violation.get("description", kind)).strip()
    if refs:
        message = f"{message} [{', '.join(r.describe() for r in refs)}]"

    context: dict[str, Any] = {"kicad_type": kind, "origin": origin}
    if primary is not None:
        if primary.component:
            context["component"] = primary.component
        if primary.net:
            context["net"] = primary.net
    unresolved = [
        item.get("uuid") for item in items if _resolve(item, index) is None
    ]
    if unresolved:
        # Say so rather than pretending: an unmapped UUID means the board holds
        # something we did not generate, which is exactly what M6 has to preserve.
        context["unmapped_uuids"] = [u for u in unresolved if u]

    hint = _HINTS.get(kind)
    if primary is None and unresolved:
        hint = (
            "this item is not one aipcb generated -- it was probably added by hand "
            "in KiCad, so the fix belongs there rather than in the source"
        )

    return Diagnostic(
        severity=severity,
        code=f"kicad-{kind.replace('_', '-')}",
        message=message,
        loc=primary.loc if primary else None,
        hint=hint,
        path=primary.path if primary else (),
        context=context,
    )


def _resolve(item: dict[str, Any], index: UuidIndex) -> SourceRef | None:
    ref = index.lookup(item.get("uuid"))
    if ref is not None:
        return ref
    # Some report items name a symbol in their description but carry a UUID we did
    # not emit -- a sheet, say. Recover the reference designator when we can.
    description = str(item.get("description", ""))
    for word in description.replace("[", " ").replace("]", " ").split():
        candidate = index.refdes(word)
        if candidate is not None:
            return candidate
    return None

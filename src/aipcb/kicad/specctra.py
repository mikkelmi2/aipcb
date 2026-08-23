# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""Specctra DSN out, Specctra SES in -- headlessly, through ``pcbnew``.

The bridge to an external router is two file formats and no API. ``aipcb`` never
*calls* Freerouting: it writes a DSN, an agent runs whatever router it likes, and
``aipcb`` reads the SES back and judges the result. This module is the half of that
which has to talk to KiCad.

Phase 0 measured which path exists, on KiCad 9.0.8, before any of this was written
(`ADR 0006 <../../../docs/decisions/0006-routing-approach.md>`_, M14e amendment):

* ``kicad-cli`` 9.0.8 has **no** DSN or SES command at all -- ``pcb`` offers only
  ``drc``, ``export`` and ``render``, and ``export`` has no Specctra format. The CLI
  has not caught up, and could not be assumed to have.
* ``pcbnew`` has both, as ``ExportSpecctraDSN`` and ``ImportSpecctraSES``, and both
  run with no display: exporting ``examples/pcie-sata`` with ``DISPLAY`` unset
  produced a 47 508-byte DSN, and importing a session file into an unrouted
  ``led-blinker`` produced tracks and saved the board.

So this module follows exactly the boundary ADR 0009 drew for the zone filler: no
``pcbnew`` in aipcb's own process, and a subprocess that imports nothing from aipcb
and nothing outside the standard library, so a system interpreter can run it.

Two things this module does that ``pcbnew`` does not:

**It protects what is already there.** KiCad exports existing copper into the DSN's
``wiring`` section with ``(type route)``, which tells the external router it may rip
all of it up. That is exactly wrong for a hybrid board: the point of sending a DSN
is to have the *remaining* nets routed, not to have M11's coupled pairs re-drawn by
something that has never heard of them. Every wire and via already on the board is
rewritten to ``(type fix)`` on the way out.

**It reports drift rather than trusting the round trip.** A session file can come
back with widths and via sizes that are not the ones the source asked for. Those are
compared against the net classes after import and reported as findings.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "PROTECTED_TYPE",
    "DsnResult",
    "SesResult",
    "SpecctraError",
    "export_dsn",
    "import_ses",
    "protect_existing_copper",
]

#: What existing copper is marked as in an exported DSN. Specctra's ``fix`` means
#: "this may not be changed", which is the only honest thing to say about copper an
#: external router knows nothing about.
PROTECTED_TYPE = "fix"

_WIRE_TYPE_RE = re.compile(r"\(type\s+route\)")


class SpecctraError(RuntimeError):
    """A DSN export or SES import failed. Carries whatever the subprocess said."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message if not detail else f"{message}\n{detail}")
        self.message = message
        self.detail = detail


@dataclass(slots=True)
class DsnResult:
    """What an export produced."""

    path: Path
    bytes_written: int = 0
    protected: int = 0
    """Pieces of existing copper marked unmovable."""
    kicad_version: str = ""
    python: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bytes": self.bytes_written,
            "protected": self.protected,
            "kicad_version": self.kicad_version,
        }


@dataclass(slots=True)
class SesResult:
    """What an import produced, as the board itself reports it."""

    board: Path
    tracks_before: int = 0
    tracks_after: int = 0
    vias_before: int = 0
    vias_after: int = 0
    nets_touched: list[str] = field(default_factory=list)
    widths_mm: dict[str, list[float]] = field(default_factory=dict)
    """Per net, the distinct track widths the session brought in."""
    via_sizes_mm: dict[str, list[list[float]]] = field(default_factory=dict)
    """Per net, the distinct ``[diameter, drill]`` pairs the session brought in."""
    kicad_version: str = ""

    @property
    def tracks_added(self) -> int:
        return self.tracks_after - self.tracks_before

    @property
    def vias_added(self) -> int:
        return self.vias_after - self.vias_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "board": str(self.board),
            "tracks_added": self.tracks_added,
            "vias_added": self.vias_added,
            "tracks_after": self.tracks_after,
            "vias_after": self.vias_after,
            "nets_touched": sorted(self.nets_touched),
            "kicad_version": self.kicad_version,
        }


# ---------------------------------------------------------------------------
# the caller side
# ---------------------------------------------------------------------------


def protect_existing_copper(text: str) -> tuple[str, int]:
    """Mark every wire and via in a DSN's ``wiring`` section unmovable.

    KiCad writes ``(type route)`` on copper it exports, which invites the external
    router to move it. Only the ``wiring`` section is touched: the same token
    appears in ``structure`` rules where it means something else entirely, and a
    blind global replace would corrupt the file.
    """
    marker = "(wiring"
    start = text.find(marker)
    if start < 0:
        return text, 0
    head, tail = text[:start], text[start:]
    patched, count = _WIRE_TYPE_RE.subn(f"(type {PROTECTED_TYPE})", tail)
    return head + patched, count


def export_dsn(
    board: Path, target: Path, *, protect: bool = True, timeout: float = 300
) -> DsnResult:
    """Write ``board`` out as a Specctra DSN.

    With ``protect`` set -- the default, and what any board with copper on it wants
    -- every piece of copper already on the board is marked unmovable before the
    file is handed over.
    """
    payload, interpreter = _run("export", str(board), str(target), timeout=timeout)
    result = DsnResult(
        path=target,
        bytes_written=int(payload.get("bytes", 0)),
        kicad_version=str(payload.get("kicad_version", "")),
        python=interpreter,
    )
    if protect and target.exists():
        text = target.read_text(encoding="utf-8", errors="replace")
        patched, count = protect_existing_copper(text)
        if count:
            target.write_text(patched, encoding="utf-8")
        result.protected = count
        result.bytes_written = target.stat().st_size
    return result


def import_ses(
    board: Path, session: Path, target: Path, *, timeout: float = 300
) -> SesResult:
    """Read a Specctra session file into ``board``, writing the result to ``target``."""
    payload, _ = _run(
        "import", str(board), str(session), str(target), timeout=timeout
    )
    return SesResult(
        board=target,
        tracks_before=int(payload.get("tracks_before", 0)),
        tracks_after=int(payload.get("tracks_after", 0)),
        vias_before=int(payload.get("vias_before", 0)),
        vias_after=int(payload.get("vias_after", 0)),
        nets_touched=[str(n) for n in payload.get("nets_touched", ())],
        widths_mm={
            str(net): [float(w) for w in widths]
            for net, widths in payload.get("widths_mm", {}).items()
        },
        via_sizes_mm={
            str(net): [[float(v) for v in pair] for pair in pairs]
            for net, pairs in payload.get("via_sizes_mm", {}).items()
        },
        kicad_version=str(payload.get("kicad_version", "")),
    )


def _run(*argv: str, timeout: float) -> tuple[dict[str, Any], str]:
    """Run this module as a script under an interpreter that can import ``pcbnew``."""
    from aipcb.kicad.cli import kicad_version as cli_version
    from aipcb.kicad.fill import (
        PCBNEW_PYTHON_ENV,
        find_pcbnew_python,
        same_version,
        version_number,
    )

    required = cli_version() or ""
    interpreter = find_pcbnew_python()
    if interpreter is None:
        raise SpecctraError(
            "no Python interpreter on this machine can import `pcbnew`, and KiCad "
            "9.0.8's kicad-cli has no Specctra command at all",
            detail=(
                "  DSN export and SES import are only reachable through pcbnew "
                "(ADR 0006, M14e amendment).\n"
                f"  Install KiCad's Python bindings, or set {PCBNEW_PYTHON_ENV} to "
                "an interpreter that has them."
            ),
        )
    if required and not same_version(required, interpreter.version):
        raise SpecctraError(
            f"KiCad version mismatch: kicad-cli is {version_number(required)} but "
            f"the pcbnew module at {interpreter.executable} is "
            f"{version_number(interpreter.version)}",
            detail=(
                "  A session file written against one version's netclasses and "
                "imported by another is exactly the kind of quiet drift this bridge "
                "exists to catch."
            ),
        )

    command = [interpreter.executable, "-m", "aipcb.kicad.specctra", *argv]
    if required:
        command += ["--require-version", required]

    environment = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent.parent)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    # The measurement in Phase 0 was made with no display, and this keeps it that
    # way: nothing here may open a window, on any machine.
    environment.pop("DISPLAY", None)
    environment.pop("WAYLAND_DISPLAY", None)

    try:
        run = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - needs a huge board
        raise SpecctraError(f"the Specctra step timed out after {timeout}s") from exc
    except OSError as exc:  # pragma: no cover - the probe already ran this binary
        raise SpecctraError(f"could not run {interpreter.executable}: {exc}") from exc

    if run.returncode != 0:
        raise SpecctraError(
            f"pcbnew could not complete the Specctra {argv[0]} (exit {run.returncode})",
            detail="\n".join(f"  {line}" for line in (run.stderr or run.stdout).splitlines()),
        )
    try:
        return json.loads(run.stdout), interpreter.executable
    except json.JSONDecodeError as exc:
        raise SpecctraError(
            "the Specctra step produced output that could not be read",
            detail=run.stdout or run.stderr,
        ) from exc


# ---------------------------------------------------------------------------
# the script side -- imports nothing from aipcb
# ---------------------------------------------------------------------------


def _same_version(required: str, found: str) -> bool:  # pragma: no cover - subprocess
    def number(text: str) -> str:
        match = re.search(r"[0-9]+(?:\.[0-9]+)*", text or "")
        return match.group(0) if match else ""

    a, b = number(required), number(found)
    return bool(a) and bool(b) and a.split(".")[:2] == b.split(".")[:2]


def _main(argv: list[str]) -> int:  # pragma: no cover - runs in the subprocess
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m aipcb.kicad.specctra",
        description="Specctra DSN export and SES import, through pcbnew.",
    )
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--require-version", default="")
    args = parser.parse_args(argv)

    try:
        import pcbnew  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"cannot import pcbnew: {exc}", file=sys.stderr)
        return 3

    built = pcbnew.GetBuildVersion()
    if args.require_version and not _same_version(args.require_version, built):
        print(
            f"KiCad version mismatch: this pcbnew is {built}, but "
            f"{args.require_version} was required.",
            file=sys.stderr,
        )
        return 4

    if args.action == "export":
        board_path, dsn_path = args.paths[0], args.paths[1]
        board = pcbnew.LoadBoard(board_path)
        if not pcbnew.ExportSpecctraDSN(board, dsn_path):
            print(f"ExportSpecctraDSN refused to write {dsn_path}", file=sys.stderr)
            return 5
        if not os.path.exists(dsn_path):
            print("ExportSpecctraDSN reported success but wrote nothing", file=sys.stderr)
            return 5
        print(
            json.dumps(
                {
                    "kicad_version": built,
                    "bytes": os.path.getsize(dsn_path),
                    "display": os.environ.get("DISPLAY", ""),
                }
            )
        )
        return 0

    board_path, session_path, target_path = args.paths[0], args.paths[1], args.paths[2]
    board = pcbnew.LoadBoard(board_path)
    before = _copper(board)
    if not pcbnew.ImportSpecctraSES(board, session_path):
        print(f"ImportSpecctraSES refused {session_path}", file=sys.stderr)
        return 6
    after = _copper(board)
    if not board.Save(target_path):
        print(f"could not save {target_path}", file=sys.stderr)
        return 7

    widths: dict[str, set[float]] = {}
    vias: dict[str, set[tuple[float, float]]] = {}
    for track in board.GetTracks():
        net = track.GetNetname()
        if track.Type() == pcbnew.PCB_VIA_T:
            vias.setdefault(net, set()).add(
                (
                    round(track.GetWidth() / 1e6, 4),
                    round(track.GetDrillValue() / 1e6, 4),
                )
            )
        else:
            widths.setdefault(net, set()).add(round(track.GetWidth() / 1e6, 4))

    print(
        json.dumps(
            {
                "kicad_version": built,
                "tracks_before": before[0],
                "vias_before": before[1],
                "tracks_after": after[0],
                "vias_after": after[1],
                "nets_touched": sorted(set(widths) | set(vias)),
                "widths_mm": {net: sorted(w) for net, w in sorted(widths.items())},
                "via_sizes_mm": {
                    net: sorted([list(pair) for pair in sizes])
                    for net, sizes in sorted(vias.items())
                },
            }
        )
    )
    return 0


def _copper(board: Any) -> tuple[int, int]:  # pragma: no cover - subprocess
    import pcbnew

    tracks = vias = 0
    for track in board.GetTracks():
        if track.Type() == pcbnew.PCB_VIA_T:
            vias += 1
        else:
            tracks += 1
    return tracks, vias


if __name__ == "__main__":  # pragma: no cover - the subprocess entry point
    sys.exit(_main(sys.argv[1:]))

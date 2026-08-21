"""Filling zones with KiCad's own engine, in a subprocess.

`kicad-cli` 9.0.8 cannot fill a zone: there is no fill command, no fill flag, and
no jobset job that fills, and `pcb drc` and `pcb export gerbers` both consume
whatever fill data is already in the file rather than producing it. That was
measured rather than inferred -- `ADR 0009 <../decisions/0009-pours.md>`_ Finding 1
-- and an unfilled pour exports as *no copper at all*, silently. The engine that
does fill is reachable from the same KiCad package, as ``pcbnew.ZONE_FILLER``.

So ADR 0009 narrows ADR 0001's "no ``pcbnew``" to **no ``pcbnew`` in the aipcb
package's own process**, and this module is that boundary. It is two things at
once:

*A script.* Run under an interpreter that can import ``pcbnew``
(``python3 -m aipcb.kicad.fill IN OUT --require-version 9.0.8``), it fills every
zone in a board and prints a JSON summary. It imports nothing from ``aipcb``
itself and nothing outside the standard library, so the system interpreter can run
it without the project's virtual environment.

*A caller.* Imported normally, :func:`fill_board` finds such an interpreter, runs
the script, and turns anything that went wrong into a :class:`FillError` carrying
``pcbnew``'s own stderr.

Three properties are load-bearing, all four of ADR 0009's conditions:

**The version lock.** The point of using KiCad's filler is that it is the same
engine ``kicad-cli`` checks against. A ``pcbnew`` from a different installation
quietly voids that, so the versions are compared before anything is filled and a
mismatch stops the run naming both. There are no silent cross-version fills.

**Explicit interpreter resolution.** The interpreter is *probed*, not assumed:
each candidate is asked to import ``pcbnew`` and report its version, and the first
that can is used. The order is documented in :data:`PYTHON_CANDIDATES`. Nothing
here relies on the accident that this machine's system Python and its virtual
environment happen to be the same 3.14.

**No silent unfilled board.** Every failure path raises. A swallowed fill error
would produce a board that passes DRC and exports with no plane copper on it,
which is exactly the class of silent wrongness this toolchain exists to remove.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PCBNEW_PYTHON_ENV",
    "PYTHON_CANDIDATES",
    "FillError",
    "FillResult",
    "PcbnewPython",
    "ZoneFill",
    "fill_board",
    "fill_project",
    "find_pcbnew_python",
    "same_version",
    "stage_project",
    "version_number",
]

#: Overrides the interpreter used to reach ``pcbnew``, for installations where the
#: probe order below does not find it.
PCBNEW_PYTHON_ENV = "AIPCB_PCBNEW_PYTHON"

#: How the interpreter that can import ``pcbnew`` is located, in order. Written
#: down rather than inferred from ``PATH`` by luck, as ADR 0009 requires:
#:
#: 1. ``AIPCB_PCBNEW_PYTHON``, when set -- the explicit answer always wins.
#: 2. ``python3`` on ``PATH``: on a distribution install, KiCad's ``pcbnew`` module
#:    is packaged for exactly this interpreter.
#: 3. ``/usr/bin/python3``, in case ``PATH`` leads somewhere else -- a virtual
#:    environment's ``python3``, most obviously.
#: 4. The interpreter running aipcb, last. It usually *cannot* import ``pcbnew``
#:    (the project's venv is built with ``include-system-site-packages = false``),
#:    but an installation that put aipcb on the system interpreter is legitimate
#:    and this is where it is caught.
#:
#: Each candidate is tried by asking it to import ``pcbnew``; none is assumed.
PYTHON_CANDIDATES = ("python3", "/usr/bin/python3")

_PROBE = "import pcbnew, sys; sys.stdout.write(pcbnew.GetBuildVersion())"

_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


class FillError(RuntimeError):
    """Filling failed. Carries whatever the subprocess said, so a check can show it."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message if not detail else f"{message}\n{detail}")
        self.message = message
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PcbnewPython:
    """An interpreter that can import ``pcbnew``, and the version it reports."""

    executable: str
    version: str

    @property
    def number(self) -> str:
        return version_number(self.version)


@dataclass(frozen=True, slots=True)
class ZoneFill:
    """What the filler made of one zone."""

    uuid: str
    net: str
    islands: int
    area_mm2: float
    vertices: int
    islands_removed: int = 0
    area_removed_mm2: float = 0.0


@dataclass(frozen=True, slots=True)
class FillResult:
    """What one fill produced. Numbers, so a report can quote them."""

    zones: int
    filled: int
    islands: int
    """Disconnected filled areas across every zone, as KiCad left them."""
    vertices: int
    removed_islands: int = 0
    """Islands island-removal deleted, when the comparison pass was asked for."""
    removed_area_mm2: float = 0.0
    per_zone: tuple[ZoneFill, ...] = ()
    kicad_version: str = ""
    python: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "zones": self.zones,
            "filled": self.filled,
            "islands": self.islands,
            "vertices": self.vertices,
            "removed_islands": self.removed_islands,
            "removed_area_mm2": round(self.removed_area_mm2, 4),
            "kicad_version": self.kicad_version,
        }


def version_number(text: str) -> str:
    """The numeric part of a KiCad version string.

    ``kicad-cli version`` says ``9.0.8``; ``pcbnew.GetBuildVersion()`` on the same
    installation says ``9.0.8+dfsg-1``, because Debian's packaging appends its
    revision. Comparing the raw strings would report a mismatch on every Debian
    machine, which is a lock that fires on the correct configuration and teaches
    people to disable it.
    """
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else ""


def same_version(a: str, b: str) -> bool:
    """Whether two KiCad version strings describe the same build, to three parts."""
    left, right = version_number(a).split("."), version_number(b).split(".")
    if not left[0] or not right[0]:
        return False
    return left[:3] == right[:3]


# ---------------------------------------------------------------------------
# the caller
# ---------------------------------------------------------------------------


def _probe(executable: str) -> PcbnewPython | None:
    try:
        run = subprocess.run(
            [executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0 or not run.stdout.strip():
        return None
    return PcbnewPython(executable, run.stdout.strip())


def find_pcbnew_python(*, refresh: bool = False) -> PcbnewPython | None:
    """The first interpreter in :data:`PYTHON_CANDIDATES` that can import ``pcbnew``.

    Cached, because the probe costs an interpreter start and the answer cannot
    change within a run.
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached if _cached.executable else None

    override = os.environ.get(PCBNEW_PYTHON_ENV)
    candidates = [override] if override else [*PYTHON_CANDIDATES, sys.executable]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) or candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        found = _probe(resolved)
        if found is not None:
            _cached = found
            return found
    _cached = PcbnewPython("", "")
    return None


_cached: PcbnewPython | None = None


def _missing_message(kicad_version: str) -> str:
    tried = ", ".join(PYTHON_CANDIDATES)
    return (
        "this design declares `pours:`, and filling them needs KiCad's own zone "
        "filler.\n"
        "  kicad-cli cannot fill zones (ADR 0009, Finding 1), so aipcb runs "
        "`pcbnew` in a subprocess.\n"
        f"  No interpreter that can `import pcbnew` was found; tried: {tried}, "
        f"{sys.executable}.\n"
        f"  Install KiCad's Python module -- the `kicad` package, not only "
        f"`kicad-cli`{f' ({kicad_version})' if kicad_version else ''} -- or point "
        f"aipcb at an interpreter that has it with "
        f"{PCBNEW_PYTHON_ENV}=/path/to/python3"
    )


def fill_board(
    source: Path,
    target: Path,
    *,
    kicad_version: str | None = None,
    measure_islands: bool = False,
    timeout: float = 600,
) -> FillResult:
    """Fill every zone in ``source``, writing the filled board to ``target``.

    ``kicad_version`` is what ``kicad-cli`` reports; the subprocess refuses to fill
    unless ``pcbnew`` agrees with it. Passing ``None`` looks it up.

    Raises :class:`FillError` for every failure -- no interpreter, a version
    mismatch, a crash inside ``pcbnew`` -- because the alternative is an unfilled
    board that nothing downstream can tell from a filled one.
    """
    from aipcb.kicad.cli import kicad_version as cli_version

    required = kicad_version if kicad_version is not None else (cli_version() or "")
    interpreter = find_pcbnew_python()
    if interpreter is None:
        raise FillError(_missing_message(required))
    if required and not same_version(required, interpreter.version):
        raise FillError(
            f"KiCad version mismatch: kicad-cli is {version_number(required)} but "
            f"the pcbnew module at {interpreter.executable} is "
            f"{version_number(interpreter.version)}.",
            detail=(
                "  Zones must be filled by the same KiCad that checks them, or DRC "
                "is checking geometry a different engine produced.\n"
                f"  Install matching versions, or set {PCBNEW_PYTHON_ENV} to the "
                "interpreter whose pcbnew matches kicad-cli."
            ),
        )

    argv = [
        interpreter.executable,
        "-m",
        "aipcb.kicad.fill",
        str(source),
        str(target),
    ]
    if required:
        argv += ["--require-version", required]
    if measure_islands:
        argv.append("--measure-islands")

    environment = dict(os.environ)
    # The system interpreter has no idea where aipcb lives; this module is designed
    # to be importable on its own, so pointing at the package root is enough.
    root = str(Path(__file__).resolve().parent.parent.parent)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root

    try:
        run = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - needs a huge board
        raise FillError(f"filling zones timed out after {timeout}s") from exc
    except OSError as exc:  # pragma: no cover - the probe already ran this binary
        raise FillError(f"could not run {interpreter.executable}: {exc}") from exc

    if run.returncode != 0:
        raise FillError(
            f"pcbnew could not fill the zones in {source.name} "
            f"(exit {run.returncode})",
            detail=_indent(run.stderr or run.stdout),
        )
    try:
        payload = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise FillError(
            "the zone filler produced output that could not be read",
            detail=_indent(run.stdout or run.stderr),
        ) from exc

    return FillResult(
        zones=int(payload["zones"]),
        filled=int(payload["filled"]),
        islands=int(payload["islands"]),
        vertices=int(payload["vertices"]),
        removed_islands=int(payload.get("removed_islands", 0)),
        removed_area_mm2=float(payload.get("removed_area_mm2", 0.0)),
        per_zone=tuple(
            ZoneFill(
                uuid=str(zone["uuid"]),
                net=str(zone["net"]),
                islands=int(zone["islands"]),
                area_mm2=float(zone["area_mm2"]),
                vertices=int(zone["vertices"]),
                islands_removed=int(zone.get("islands_removed", 0)),
                area_removed_mm2=float(zone.get("area_removed_mm2", 0.0)),
            )
            for zone in payload.get("per_zone", ())
        ),
        kicad_version=str(payload.get("kicad_version", "")),
        python=interpreter.executable,
    )


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in (text or "").strip().splitlines())


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------

#: Files KiCad reads *alongside* a board and would silently do without: the project
#: file carries the design rules DRC enforces, the schematic is what
#: ``--schematic-parity`` compares against, and the library tables resolve names.
_PROJECT_SIBLINGS = ("fp-lib-table", "sym-lib-table")


def stage_project(board: Path, staging: Path) -> Path:
    """Copy a built project into ``staging`` and return the copy of the board.

    Filling happens on a copy so that build output stays the unfilled reference --
    that is M10b's stability policy expressed as a directory rather than a promise.
    The *whole project* is copied, not just the board, because a board checked
    without its ``.kicad_pro`` is checked against KiCad's default design rules
    rather than the ones the source asked for, and it would pass.
    """
    staging.mkdir(parents=True, exist_ok=True)
    for sibling in board.parent.iterdir():
        if not sibling.is_file():
            continue
        if sibling.stem == board.stem or sibling.name in _PROJECT_SIBLINGS:
            shutil.copy2(sibling, staging / sibling.name)
    return staging / board.name


def fill_project(
    board: Path,
    staging: Path,
    *,
    kicad_version: str | None = None,
    measure_islands: bool = False,
) -> tuple[Path, FillResult]:
    """Stage a project and fill its board. Returns the filled board and the numbers."""
    staged = stage_project(board, staging)
    result = fill_board(
        board,
        staged,
        kicad_version=kicad_version,
        measure_islands=measure_islands,
    )
    return staged, result


# ---------------------------------------------------------------------------
# the script -- everything below runs under the interpreter that has pcbnew
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:  # pragma: no cover - runs in the subprocess
    """Fill a board. Deliberately dependency-free beyond ``pcbnew`` itself."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m aipcb.kicad.fill",
        description="Fill every zone in a KiCad board with KiCad's own engine.",
    )
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument(
        "--require-version",
        default="",
        help="Refuse to fill unless pcbnew reports this KiCad version.",
    )
    parser.add_argument(
        "--measure-islands",
        action="store_true",
        help="Also fill with island removal disabled, to report what it deleted.",
    )
    args = parser.parse_args(argv)

    try:
        import pcbnew  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"cannot import pcbnew: {exc}", file=sys.stderr)
        return 3

    built = pcbnew.GetBuildVersion()
    if args.require_version and not same_version(args.require_version, built):
        print(
            f"KiCad version mismatch: this pcbnew is {built}, but "
            f"{args.require_version} was required. Zones must be filled by the "
            f"same KiCad that checks them; refusing to fill.",
            file=sys.stderr,
        )
        return 4

    board = pcbnew.LoadBoard(args.source)
    zones = list(board.Zones())

    baseline: dict[int, tuple[int, float]] = {}
    if args.measure_islands:
        baseline = _fill_without_island_removal(pcbnew, board, zones)

    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    board.Save(args.target)

    per_zone: list[dict[str, object]] = []
    islands = vertices = filled = 0
    removed_islands = 0
    removed_area = 0.0
    for index, zone in enumerate(zones):
        count, area, vertex_count = _measure(zone, pcbnew.PCB_IU_PER_MM)
        islands += count
        vertices += vertex_count
        filled += 1 if count else 0
        was_count, was_area = baseline.get(index, (count, area))
        removed_islands += max(was_count - count, 0)
        removed_area += max(was_area - area, 0.0)
        per_zone.append(
            {
                "uuid": zone.m_Uuid.AsString(),
                "net": zone.GetNetname(),
                "islands": count,
                "area_mm2": round(area, 6),
                "vertices": vertex_count,
                "islands_removed": max(was_count - count, 0),
                "area_removed_mm2": round(max(was_area - area, 0.0), 6),
            }
        )

    json.dump(
        {
            "zones": len(zones),
            "filled": filled,
            "islands": islands,
            "vertices": vertices,
            "removed_islands": removed_islands,
            "removed_area_mm2": round(removed_area, 6),
            "per_zone": per_zone,
            "kicad_version": built,
        },
        sys.stdout,
    )
    return 0


def _measure(
    zone: object, per_mm: float
) -> tuple[int, float, int]:  # pragma: no cover - subprocess
    """One zone's filled islands, area in mm^2 and vertex count, over every layer.

    ``per_mm`` is ``pcbnew.PCB_IU_PER_MM``, passed in rather than imported so this
    module has exactly one ``import pcbnew`` and it is the one inside the guard.
    """
    islands = vertices = 0
    area = 0.0
    for layer in zone.GetLayerSet().Seq():  # type: ignore[attr-defined]
        polys = zone.GetFilledPolysList(layer)  # type: ignore[attr-defined]
        if polys is None:
            continue
        islands += polys.OutlineCount()
        area += polys.Area() / (per_mm * per_mm)
        for outline in range(polys.OutlineCount()):
            vertices += polys.Outline(outline).PointCount()
    return islands, area, vertices


def _fill_without_island_removal(
    pcbnew: object, board: object, zones: list[object]
) -> dict[int, tuple[int, float]]:  # pragma: no cover - subprocess
    """Fill once with island removal off, to learn what removal will later delete.

    The only honest way to say "island removal took copper off this plane" is to
    look at the plane with removal switched off and compare. Doing it in the same
    process costs one extra ``Fill()`` and no extra interpreter start; doing it by
    arithmetic on the zone outline would mean reimplementing the filler, which
    ADR 0009 rejects.
    """
    saved = [zone.GetIslandRemovalMode() for zone in zones]  # type: ignore[attr-defined]
    never = pcbnew.ISLAND_REMOVAL_MODE_NEVER  # type: ignore[attr-defined]
    for zone in zones:
        zone.SetIslandRemovalMode(never)  # type: ignore[attr-defined]
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())  # type: ignore[attr-defined]
    per_mm = pcbnew.PCB_IU_PER_MM  # type: ignore[attr-defined]
    measured = {
        index: (count, area)
        for index, zone in enumerate(zones)
        for count, area, _ in [_measure(zone, per_mm)]
    }
    for zone, mode in zip(zones, saved, strict=True):
        zone.SetIslandRemovalMode(mode)  # type: ignore[attr-defined]
    return measured


if __name__ == "__main__":  # pragma: no cover - the subprocess entry point
    sys.exit(_main(sys.argv[1:]))

"""Running one slice through openEMS, and not running it again if nothing changed.

The solver never touches the host: it lives in the pinned container ADR 0011
records, and this module hands it a working directory and reads what comes back.
Two things here earn their keep more than the subprocess call does.

**Caching.** A slice's identity is the hash of what the solver actually reads --
the slice board, ``simulation.json`` and ``stackup.json``. Re-running an unchanged
pair costs nothing, which is what makes an edit-and-resimulate loop usable at
30 seconds to two minutes a pair.

**Asserting the inputs were seen.** Phase 0 found two failure modes that produce a
confident wrong answer rather than an error: a placement file the consumer never
finds (so no ports), and drill coordinates it cannot parse (so no vias). Both fail
*silently*, and a zero exit code says nothing about either. So every run is checked
against the log: the ports it was given must appear, and the vias in the drill file
must appear. A run whose inputs went unread is a failure here even when gerber2ems
is happy.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.si import IMAGE

__all__ = [
    "ContainerMissing",
    "RunOutcome",
    "container_digest",
    "find_container",
    "nets_in_gerbers",
    "run_gerber2ems",
    "slice_digest",
]

#: How long one pair may take before it is called a failure rather than a wait.
#: Phase 0's 30 s - 2 min was measured on a two-layer slice with no pour; M12's
#: boards are four-layer with planes on both sides of the signal, and the measured
#: range at the shipped mesh density is 1 to 13 minutes a pair. Half an hour is
#: comfortably clear of the worst of those and still bounds an unattended batch.
DEFAULT_TIMEOUT_S = 1800


class ContainerMissing(RuntimeError):
    """No container runtime, or no image. Carries what to do about it."""


@dataclass(slots=True)
class RunOutcome:
    """What one solver run produced."""

    ok: bool
    seconds: float
    cells: int | None = None
    timesteps: int | None = None
    ports_seen: int = 0
    vias_seen: int = 0
    energy_db: float | None = None
    message: str = ""
    log: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "seconds": round(self.seconds, 2),
            "cells": self.cells,
            "timesteps": self.timesteps,
            "ports_seen": self.ports_seen,
            "vias_seen": self.vias_seen,
            "energy_db": self.energy_db,
            "message": self.message,
            "warnings": list(self.warnings),
        }


def find_container() -> str:
    """``podman`` or ``docker``, whichever is on PATH. Podman first, as ADR 0011 chose."""
    for candidate in ("podman", "docker"):
        found = shutil.which(candidate)
        if found:
            return found
    raise ContainerMissing(
        "no container runtime found on PATH (looked for podman and docker)"
    )


def container_digest(runtime: str, image: str = IMAGE) -> str:
    """The image's config digest, so a run can report what it ran on."""
    run = subprocess.run(
        [runtime, "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise ContainerMissing(
            f"the image {image} is not present. Build it from Antmicro's "
            "gerber2ems Dockerfile with `--format docker` (ADR 0011 records the "
            "pinned commits and the reason for that flag)"
        )
    digest = run.stdout.strip()
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"


def slice_digest(work: Path) -> str:
    """The identity of a slice: everything the solver reads, hashed in a fixed order.

    Deliberately *not* the whole working directory -- ``ems/`` is output, and the
    Gerbers are a pure function of the board, so hashing them as well would only
    make the digest depend on kicad-cli's version. What is hashed is what a change
    to the design or the parameters actually moves.
    """
    digest = hashlib.sha256()
    for name in ("slice.kicad_pcb", "simulation.json", "fab/stackup.json"):
        path = work / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()


_CELLS = re.compile(r"Rectilinear grid: ([\d,]+) cells|Cell count: ([\d,]+)")
_PORT_FOUND = re.compile(r"Found port #(\d+) position in pos file")
_ADDING_PORT = re.compile(r"Adding port at start")
_VIAS = re.compile(r"Found (\d+) vias")
_ENERGY = re.compile(r"Energy: ~[\d.e+-]+ \(-([\d.]+)dB\)")


def _parse_log(log: str) -> tuple[int | None, int | None, int, int, float | None]:
    cells = timesteps = None
    for line in log.splitlines():
        if "Cells =" in line:
            match = re.search(r"Cells\s*=\s*(\d+)", line)
            if match:
                cells = int(match.group(1))
        if "Number of timesteps" in line:
            match = re.search(r"(\d+)", line.split("Number of timesteps")[1])
            if match:
                timesteps = int(match.group(1))
    ports = len(set(_PORT_FOUND.findall(log))) or len(_ADDING_PORT.findall(log))
    vias_match = _VIAS.findall(log)
    vias = max((int(v) for v in vias_match), default=0)
    energies = [float(e) for e in _ENERGY.findall(log)]
    return cells, timesteps, ports, vias, (max(energies) if energies else None)


def run_gerber2ems(
    work: Path,
    *,
    runtime: str,
    image: str = IMAGE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    expect_ports: int = 4,
    expect_vias: int = 0,
    log_path: Path | None = None,
) -> RunOutcome:
    """Geometry, both excitations and post-processing, in one container run."""
    started = time.monotonic()
    command = [
        runtime,
        "run",
        "--rm",
        "--userns=keep-id",
        "-v",
        f"{work.resolve()}:/work",
        "-w",
        "/work",
        image,
        "-a",
        "--log",
        "DEBUG",
    ]
    try:
        run = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return RunOutcome(
            ok=False,
            seconds=time.monotonic() - started,
            message=f"the solver did not finish inside {timeout_s} s",
        )
    seconds = time.monotonic() - started
    log = run.stdout + run.stderr
    if log_path is not None:
        log_path.write_text(log, encoding="utf-8")

    cells, timesteps, ports, vias, energy = _parse_log(log)
    warnings: list[str] = []
    ok = run.returncode == 0

    message = ""
    if not ok:
        tail = [line for line in log.splitlines() if line.strip()][-1:]
        message = tail[0].strip() if tail else f"exit status {run.returncode}"

    # The silent-failure checks. A zero exit code proves the program ran, not that
    # it read what it was handed.
    if ok and ports < expect_ports:
        ok = False
        message = (
            f"gerber2ems found {ports} of {expect_ports} simulation ports; the "
            "placement file was not read in the frame it expects"
        )
    if ok and expect_vias and vias < expect_vias:
        ok = False
        message = (
            f"gerber2ems found {vias} of {expect_vias} vias in the drill file; "
            "negative or unparsable coordinates are dropped without an error"
        )
    if energy is not None and energy < 30:
        warnings.append(
            f"the simulation stopped with only {energy:.0f} dB of energy decay; "
            "the result is not converged in time"
        )
    return RunOutcome(
        ok=ok,
        seconds=seconds,
        cells=cells,
        timesteps=timesteps,
        ports_seen=ports,
        vias_seen=vias,
        energy_db=energy,
        message=message,
        log=log,
        warnings=warnings,
    )


_GERBER_NET = re.compile(r"%TO\.N,([^*]*)\*%")


def nets_in_gerbers(fab: Path) -> set[str]:
    """Every net name the copper Gerbers actually carry, read back out of them.

    The X2 net attribute is what gerber2ems's mesh generator keys on to decide which
    copper to resolve finely. It is also the one part of the export that a *correct*
    board can lose: KiCad prunes nets nothing references when it loads a slice, and
    the tracks come back labelled with a neighbour's name. The geometry is fine, the
    mesh is not, and the run exits zero. So the export is read back rather than
    trusted.
    """
    found: set[str] = set()
    for path in sorted(fab.glob("*_Cu.gbr")):
        found.update(_GERBER_NET.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}

"""Running one slice through openEMS, and not running it again if nothing changed.

The solver never touches the host: it lives in the pinned container ADR 0011
records, and this module hands it a working directory and reads what comes back.
Two things here earn their keep more than the subprocess call does.

**Caching.** A slice's identity is the hash of what the solver actually reads --
the slice board, ``simulation.json`` and ``stackup.json``. Re-running an unchanged
pair costs nothing, which is what makes an edit-and-resimulate loop usable at
30 seconds to two minutes a pair.

**Reaping the container, whatever happens to the client.** The solver runs for
minutes on every core the machine has, and it is a *container* -- a child of the
runtime's daemon, not of this process, so nothing about this process dying stops
it. M12 shipped cleanup on the timeout path only, and the M10-M12 chain paid for
that twice: a killed session left a sixteen-core FDTD run going eleven minutes
later, and a relaunch then had two containers writing one working directory. M13d
closes it three ways -- a context manager that reaps on any exit, signal and
`atexit` handlers that reap on interruption, and a *pre-flight* check that refuses
to start a second run against a directory something is already writing. The last
of the three is the one that survives ``SIGKILL``, which nothing can catch.

**Asserting the inputs were seen.** Phase 0 found two failure modes that produce a
confident wrong answer rather than an error: a placement file the consumer never
finds (so no ports), and drill coordinates it cannot parse (so no vias). Both fail
*silently*, and a zero exit code says nothing about either. So every run is checked
against the log: the ports it was given must appear, and the vias in the drill file
must appear. A run whose inputs went unread is a failure here even when gerber2ems
is happy.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aipcb.si import IMAGE

__all__ = [
    "CPUS_PER_SOLVER",
    "ContainerBusy",
    "ContainerMissing",
    "RunOutcome",
    "arm_cleanup",
    "container_digest",
    "containers_on",
    "cpu_slots",
    "default_parallel",
    "find_container",
    "nets_in_gerbers",
    "reap_containers",
    "run_gerber2ems",
    "running_container",
    "slice_digest",
    "supports_cpuset",
]

#: How long one pair may take before it is called a failure rather than a wait.
#: The floor under :attr:`aipcb.model.simulation.SimulationSettings.timeout_s`,
#: which is what a design and ``--timeout`` actually set; this is only what a
#: direct call to :func:`run_gerber2ems` gets.
#:
#: **Raised from 1800 s at M13.6, and the old number is worth keeping in view.**
#: 1800 was set from M12's measured 1 to 13 minutes a pair, which was honest for
#: the boards M12 ran. M13b then narrowed `examples/pcie-sata`'s traces, which
#: refined the mesh, which multiplied `max_steps` -- and every SATA link on that
#: board moved past the limit. M13 left a batch running against it overnight and
#: it reported ``failed 1800.0 s`` twice rather than a number, because a timeout
#: produces no ``result.json`` at all: the next run re-slices and starts from zero.
#:
#: The measurements this is sized from, all on the same sixteen-core machine:
#:
#: ==========================  ===========  =============================
#: link                        wall clock   conditions
#: ==========================  ===========  =============================
#: ``SATA0_RXN/P`` (M13.5)     3 216 s      test suite running beside it
#: ``SATA0_RXN/P`` (projected) ~2 220 s     machine to itself, 79 steps/s
#: ``REFCLKN/P`` (M13.6)       ~430 s       machine to itself, full steps
#: ==========================  ===========  =============================
#:
#: 7200 s is a shade over twice the slowest thing anyone has measured here. It is
#: deliberately not close to the typical run: the cost of setting it too high is
#: waiting, and the cost of setting it too low is an empty directory after an hour.
DEFAULT_TIMEOUT_S = 7200


#: How many host CPUs one solver process is given when a batch runs several at once.
#:
#: **A measurement, and it replaces an assumption.** ADR 0011 Decision 4 declined
#: process-level parallelism because "openEMS already uses every core -- it reported
#: 750-1270 MCells/s on this machine's sixteen". M13.6 read the solver's own log on
#: every link of an eleven-link batch and found the opposite: the multithreaded
#: engine benchmarks itself at startup and settles on **four to five** of sixteen,
#: at 256-451 MC/s. M13.7 re-measured on an idle machine and saw **six threads at
#: 619 MC/s median** -- so the auto-tuner's answer moves with the load and with the
#: model, and what it never does is use the machine.
#:
#: Five is the middle of what has been observed. It is not a thread count this code
#: can set -- gerber2ems calls ``openEMS.Run()`` without ``numThreads``, and the
#: image is pinned -- it is the size of the *cpuset* each container gets, inside
#: which openEMS runs its own benchmark and picks its own number.
#:
#: **What it buys, measured rather than assumed: on this machine, nothing.** Three
#: concurrent solvers came back 48 % slower than three sequential ones and their
#: aggregate throughput was below a single solver's, because what the spare cores
#: are short of is memory bandwidth and a second process does not create any. So
#: this is the width of a slot for the caller who asks for one, not a default. ADR
#: 0011 Decision 4a has the table.
CPUS_PER_SOLVER = 5


class ContainerMissing(RuntimeError):
    """No container runtime, or no image. Carries what to do about it."""


class ContainerBusy(RuntimeError):
    """Something is already solving in this working directory."""


#: The label every container this module starts carries, holding the absolute
#: working directory it was given. A *label* rather than a name, because the name
#: has to be unique per run and the question the pre-flight asks is "who else is
#: writing here", which is a property of the directory.
WORK_LABEL = "aipcb.si.work"


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
    mcells_per_s: float | None = None
    """Median of the solver's own throughput samples. What a parallel run is judged on."""
    threads: int | None = None
    """How many the engine's startup benchmark settled on, inside whatever it was given."""
    cpus: str = ""
    """The cpuset this run was pinned to, empty when it had the machine."""
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
            "mcells_per_s": self.mcells_per_s,
            "threads": self.threads,
            "cpus": self.cpus,
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


#: Every phrasing openEMS and gerber2ems have used for the mesh size. The one this
#: image actually prints is the third -- ``FDTD simulation size: 81x277x42 -->
#: 942354 FDTD cells`` -- and until M13.7 none of the patterns matched it, so
#: ``RunOutcome.cells`` was ``None`` on every run this tool has ever done.
#:
#: The number is read as a float and rounded, because openEMS switches notation on
#: its own: a 0.94 M mesh prints ``942354`` and a 1.37 M one prints ``1.37063e+06``,
#: in the same line of the same log. Matching integers only would have half-fixed
#: this and looked fixed.
_CELLS = re.compile(
    r"Rectilinear grid: ([\d,.e+]+) cells"
    r"|Cell count: ([\d,.e+]+)"
    r"|-->\s*([\d,.e+]+) FDTD cells"
    r"|Cells\s*=\s*([\d,.e+]+)"
)
_PORT_FOUND = re.compile(r"Found port #(\d+) position in pos file")
_ADDING_PORT = re.compile(r"Adding port at start")
_VIAS = re.compile(r"Found (\d+) vias")
_ENERGY = re.compile(r"Energy: ~[\d.e+-]+ \(-([\d.]+)dB\)")
_SPEED = re.compile(r"Speed:\s*([\d.]+) MC/s")
_THREADS = re.compile(r"Best performance found using (\d+) threads")


def _throughput(log: str) -> tuple[float | None, int | None]:
    """``(median MC/s, threads the engine chose)`` from the solver's own progress lines.

    The median rather than the mean: the first sample of every run is taken while
    the engine is still benchmarking itself and reads about half of what the run
    settles at, and one warm-up sample should not move the number a measurement is
    read off.
    """
    speeds = sorted(float(v) for v in _SPEED.findall(log))
    threads = [int(v) for v in _THREADS.findall(log)]
    return (
        speeds[len(speeds) // 2] if speeds else None,
        max(threads) if threads else None,
    )


def _parse_log(log: str) -> tuple[int | None, int | None, int, int, float | None]:
    cells = timesteps = None
    for line in log.splitlines():
        found = _CELLS.search(line)
        if found:
            cells = round(float(next(g for g in found.groups() if g).replace(",", "")))
        if "Number of timesteps" in line:
            match = re.search(r"(\d+)", line.split("Number of timesteps")[1])
            if match:
                timesteps = int(match.group(1))
    ports = len(set(_PORT_FOUND.findall(log))) or len(_ADDING_PORT.findall(log))
    vias_match = _VIAS.findall(log)
    vias = max((int(v) for v in vias_match), default=0)
    energies = [float(e) for e in _ENERGY.findall(log)]
    return cells, timesteps, ports, vias, (max(energies) if energies else None)


# ---------------------------------------------------------------------------
# container lifetime
# ---------------------------------------------------------------------------

#: Containers this process has started and not yet reaped. Kept so that a signal
#: handler, which cannot be handed arguments, still knows what to kill.
_LIVE: dict[str, str] = {}

_HANDLERS_INSTALLED = False


def reap_containers(runtime: str | None = None) -> list[str]:
    """Force-remove every container this process started and has not finished.

    Returns what it reaped, so a caller -- or a test -- can say so. Safe to call
    twice, and safe to call when there is nothing to reap.
    """
    reaped: list[str] = []
    for name, started_with in list(_LIVE.items()):
        _LIVE.pop(name, None)
        try:
            subprocess.run(
                [runtime or started_with, "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
            continue
        reaped.append(name)
    return reaped


def _install_handlers() -> None:
    """Reap on ``atexit`` and on the two signals a person or a batch system sends.

    ``SIGKILL`` cannot be caught, and that is exactly the case the pre-flight check
    in :func:`run_gerber2ems` exists for. What these catch is the common one: a
    Ctrl-C, or a supervisor stopping the job.
    """
    global _HANDLERS_INSTALLED
    if _HANDLERS_INSTALLED:
        return
    _HANDLERS_INSTALLED = True
    atexit.register(reap_containers)

    def on_signal(number: int, frame: object) -> None:
        reap_containers()
        previous = _PREVIOUS.get(number)
        if callable(previous):
            previous(number, frame)
            return
        # Re-raise as the default would have, so the exit status still says the
        # process was signalled rather than that it chose to stop.
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)

    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            _PREVIOUS[number] = signal.getsignal(number)
            signal.signal(number, on_signal)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            _PREVIOUS.pop(number, None)


_PREVIOUS: dict[int, object] = {}


def containers_on(runtime: str, work: Path) -> list[str]:
    """Names of running containers this tool started against ``work``.

    The pre-flight. Two solvers writing one ``ems/`` produce results that belong to
    neither, and the second one to arrive has no way to tell -- the files simply
    change under it. Asking the runtime costs about ten milliseconds.
    """
    run = subprocess.run(
        [
            runtime,
            "ps",
            "--filter",
            f"label={WORK_LABEL}={work.resolve()}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        return []
    return [line.strip() for line in run.stdout.splitlines() if line.strip()]


#: Answer per ``(runtime, image)``, because the probe costs a container start and
#: the answer is a property of the host's cgroup delegation, which does not move
#: inside a run.
_CPUSET_SUPPORT: dict[tuple[str, str], bool] = {}


def supports_cpuset(runtime: str, image: str = IMAGE) -> bool:
    """Whether this runtime can actually pin a container to a set of CPUs.

    Measured rather than assumed, and the measurement is why: **rootless podman on
    a stock cgroup v2 host cannot.** ``--cpuset-cpus`` is accepted by the CLI and
    then refused by the OCI runtime --

    ``crun: controller `cpuset` is not available under /sys/fs/cgroup/...``

    -- because systemd delegates ``cpu memory pids`` to the user slice and not
    ``cpuset``. Granting it means a ``Delegate=`` drop-in on ``user@.service``,
    which is a root change to somebody's machine and not something a PCB tool
    should require. So a batch asks first and runs unpinned if the answer is no:
    concurrent solvers on a shared pool still finish sooner than one after another,
    they just share cores instead of dividing them.
    """
    cached = _CPUSET_SUPPORT.get((runtime, image))
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [runtime, "run", "--rm", "--cpuset-cpus", "0", "--entrypoint",
             "/bin/true", image],
            capture_output=True, text=True, check=False, timeout=120,
        )
        answer = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no runtime
        answer = False
    _CPUSET_SUPPORT[(runtime, image)] = answer
    return answer


def arm_cleanup() -> None:
    """Install the reapers now, from the caller's thread.

    :func:`running_container` installs them lazily, and ``signal.signal`` only works
    on the main thread -- so a batch that first starts a container from a worker
    would get the ``atexit`` handler and silently lose the Ctrl-C one. A batch calls
    this before it dispatches anything.
    """
    _install_handlers()


def default_parallel(cpus: int | None = None) -> int:
    """How many solvers to run at once when nobody says: one per :data:`CPUS_PER_SOLVER`.

    Floored at one, so a small machine runs the sequential path it always ran.
    """
    available = cpus if cpus is not None else (os.cpu_count() or 1)
    return max(1, available // CPUS_PER_SOLVER)


def cpu_slots(parallel: int, cpus: int | None = None) -> list[str]:
    """One ``--cpuset-cpus`` string per concurrent solver, disjoint and contiguous.

    Disjoint because the point is that two solvers stop competing: a quota
    (``--cpus``) throttles a container that still *sees* every CPU and still spawns
    threads for all of them, and openEMS sizes its startup benchmark off what it
    sees. A cpuset changes what it sees.

    Contiguous rather than interleaved, which is a deliberate simplification worth
    naming: on a hybrid part -- this machine is ten cores presenting sixteen CPUs,
    some of them SMT siblings and some efficiency cores -- consecutive numbers are
    not equal cores, so the slots are not equally fast. Making them equal means
    reading the topology, and the measurement below did not need it.
    """
    available = cpus if cpus is not None else (os.cpu_count() or 1)
    if parallel <= 1 or available < 2 * parallel:
        return [""] * max(parallel, 1)
    width = available // parallel
    return [f"{i * width}-{i * width + width - 1}" for i in range(parallel)]


@contextmanager
def running_container(runtime: str, name: str) -> Iterator[None]:
    """Own one container for the duration of a block, and reap it on the way out.

    On *any* way out: a return, an exception, a timeout, a ``KeyboardInterrupt``.
    ``podman run --rm`` already removes a container that exits on its own; what
    this covers is every path where it does not get to.
    """
    _install_handlers()
    _LIVE[name] = runtime
    try:
        yield
    finally:
        if _LIVE.pop(name, None) is not None:
            subprocess.run(
                [runtime, "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )


def run_gerber2ems(
    work: Path,
    *,
    runtime: str,
    image: str = IMAGE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    expect_ports: int = 4,
    expect_vias: int = 0,
    log_path: Path | None = None,
    cpus: str = "",
) -> RunOutcome:
    """Geometry, both excitations and post-processing, in one container run.

    Raises :class:`ContainerBusy` when something is already solving in ``work``.
    That is a refusal rather than a wait on purpose: the other run is minutes from
    finishing, its results are about to land in this directory, and starting a
    second writer produces a directory whose contents belong to neither.
    """
    started = time.monotonic()
    busy = containers_on(runtime, work)
    if busy:
        raise ContainerBusy(
            f"{', '.join(busy)} is already solving in {work}; a second solver "
            "writing the same directory produces results that belong to neither. "
            f"Wait for it, or `{runtime} rm -f {busy[0]}` if it is an orphan left "
            "by a killed run."
        )
    # Named, so a timeout can clean up after itself; labelled with the working
    # directory, so the pre-flight above can find it whoever started it. Killing
    # the client is not the same as stopping the container, and an unattended batch
    # that leaves a sixteen-core FDTD run behind takes the machine down with it.
    name = f"aipcb-si-{os.getpid()}-{abs(hash(str(work))) % 10**8:08d}"
    command = [
        runtime,
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        f"{WORK_LABEL}={work.resolve()}",
        "--userns=keep-id",
        *(["--cpuset-cpus", cpus] if cpus else []),
        "-v",
        f"{work.resolve()}:/work",
        "-w",
        "/work",
        image,
        "-a",
        "--log",
        "DEBUG",
    ]
    with running_container(runtime, name):
        try:
            run = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(
                ok=False,
                seconds=time.monotonic() - started,
                cpus=cpus,
                message=f"the solver did not finish inside {timeout_s} s",
            )
    seconds = time.monotonic() - started
    log = run.stdout + run.stderr
    if log_path is not None:
        log_path.write_text(log, encoding="utf-8")

    cells, timesteps, ports, vias, energy = _parse_log(log)
    speed, threads = _throughput(log)
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
        mcells_per_s=speed,
        threads=threads,
        cpus=cpus,
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

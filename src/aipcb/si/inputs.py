"""The three files gerber2ems reads besides the Gerbers, and how they are derived.

``aipcb export`` produces a fabrication package. gerber2ems wants that package plus
three descriptions no board house needs: what the laminate is made of, what to
solve for, and which nets are under test. All three are derived -- nothing here asks
the designer for a number aipcb already knows.

The one that matters most is ``stackup.json``, and it is derived from the *source*
stackup rather than transcribed out of the ``.kicad_pcb``. That is a deliberate
choice with a defect behind it: ``compile/board.py`` writes KiCad a stackup of
uniformly-thick dielectrics, while ``model/layout.py`` derives impedance from the
``layers:`` block the source declares. On ``examples/pcie-sata`` those disagree by
more than a factor of two -- 0.48 mm uniform against a declared 0.2104 mm prepreg
under F.Cu -- and simulating the KiCad copy would measure a board nobody described.
The source block wins because it is the one the impedance target came from. The
disagreement itself is a finding, recorded in the M12 report.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from aipcb.model.layout import (
    DEFAULT_LOSS_TANGENT,
    MASK_THICKNESS_MM,
    Stackup,
    copper_layer_names,
)
from aipcb.model.simulation import ResolvedSimulation
from aipcb.si.slice import Slice

__all__ = [
    "Dielectric",
    "dielectrics",
    "netinfo_json",
    "simulation_json",
    "stackup_json",
    "write_inputs",
]

#: gerber2ems rasterises every Gerber before meshing it. Five micrometres per pixel
#: is its own default and resolves a 0.2 mm trace across forty pixels.
PIXEL_SIZE_UM = 5.0


@dataclass(frozen=True, slots=True)
class Dielectric:
    """One laminate layer between two copper layers."""

    index: int
    thickness_mm: float
    epsilon_r: float
    loss_tangent: float
    material: str


def dielectrics(stackup: Stackup) -> list[Dielectric]:
    """The laminate between each adjacent pair of copper layers, front to back."""
    metals = copper_layer_names(stackup.copper_layers)
    declared = stackup.declared_stack
    out: list[Dielectric] = []
    for index in range(len(metals) - 1):
        between = stackup.dielectric_between(metals[index], metals[index + 1])
        material = "FR4"
        loss = DEFAULT_LOSS_TANGENT
        if declared is not None:
            seen = -1
            for entry in declared:
                if entry.type == "copper":
                    seen += 1
                    continue
                if seen == index:
                    material = entry.material or material
                    if entry.loss_tangent is not None:
                        loss = entry.loss_tangent
                    break
        out.append(
            Dielectric(
                index=index + 1,
                thickness_mm=between.thickness_mm,
                epsilon_r=between.epsilon_r,
                loss_tangent=loss,
                material=material,
            )
        )
    return out


def _layer(name: str, kind: str, **rest: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "type": kind,
        "color": None,
        "thickness": None,
        "material": None,
        "epsilon": None,
        "lossTangent": None,
        "user-name": name.replace(".", "_").replace("SilkS", "Silkscreen"),
    }
    entry.update(rest)
    return entry


def stackup_json(stackup: Stackup) -> dict[str, Any]:
    """The stackup in gerber2ems's ``format_version`` 1.0 shape.

    Layer *order* is the load-bearing part: gerber2ems walks the list front to back
    and accumulates a z offset from each substrate's thickness, so an out-of-order
    list silently puts the trace on the wrong side of its plane.
    """
    metals = copper_layer_names(stackup.copper_layers)
    layers: list[dict[str, Any]] = [
        _layer("F.SilkS", "Top Silk Screen"),
        _layer("F.Paste", "Top Solder Paste"),
        _layer("F.Mask", "Top Solder Mask", thickness=MASK_THICKNESS_MM),
    ]
    laminate = dielectrics(stackup)
    for index, metal in enumerate(metals):
        layers.append(
            _layer(metal, "copper", thickness=stackup.copper_thickness_mm(metal))
        )
        if index < len(laminate):
            entry = laminate[index]
            layers.append(
                _layer(
                    f"dielectric {entry.index}",
                    "core",
                    thickness=entry.thickness_mm,
                    material=entry.material,
                    epsilon=entry.epsilon_r,
                    lossTangent=entry.loss_tangent,
                )
            )
    layers += [
        _layer("B.Mask", "Bottom Solder Mask", thickness=MASK_THICKNESS_MM),
        _layer("B.Paste", "Bottom Solder Paste"),
        _layer("B.SilkS", "Bottom Silk Screen"),
    ]
    return {"layers": layers, "format_version": "1.0"}


def simulation_json(sliced: Slice, settings: ResolvedSimulation) -> dict[str, Any]:
    """What to solve, in gerber2ems's ``format_version`` 1.2 shape.

    Every length in this file is micrometres, which is not written anywhere in the
    upstream README and is the single easiest thing to get wrong by a factor of a
    thousand.
    """
    ports = [
        {
            "name": f"SP{port.number}",
            "width": round(port.width_mm * 1000, 1),
            "length": round(port.length_mm * 1000, 1),
            "impedance": round(port.impedance_ohm, 3),
            "layer": port.layer_index,
            "plane": port.plane_index,
            # Ports 1 and 3 are the two halves of the driven end. Exciting both, one
            # run each, is what makes a four-port S-matrix and therefore what makes
            # the differential mode extractable at all.
            "excite": port.number in (1, 3),
        }
        for port in sliced.ports
    ]
    return {
        "format_version": "1.2",
        "frequency": {"start": settings.start_hz, "stop": settings.stop_hz},
        "max_steps": max_steps(sliced, settings),
        "pixel_size": PIXEL_SIZE_UM,
        "ports": ports,
        "differential_pairs": [
            {
                "start_p": 0,
                "stop_p": 1,
                "start_n": 2,
                "stop_n": 3,
                "name": sliced.pair.name,
                "nets": list(sliced.pair.nets),
            }
        ],
        "grid": {
            "inter_layers": settings.grid_inter_layers,
            "optimal": grid_optimal_um(sliced, settings),
            "diagonal": max(grid_optimal_um(sliced, settings), 50.0),
            "perpendicular": 100.0,
            "max": 400.0,
            "cell_ratio": {"xy": 1.2, "z": 1.5},
            "margin": {"xy": 1000.0, "z": 2000.0, "from_trace": True},
        },
    }


#: How many cells the mesh must put across the narrowest feature under test --
#: the trace, and the gap between the two halves.
#:
#: M13b's coplanar model derives narrower traces than the bare-microstrip one did,
#: and the fixed 50 um default M12 calibrated on `examples/mcu-4layer`'s 0.25 mm
#: pair turned out to be **silently catastrophic** on the 0.185 mm one it now
#: derives for `examples/pcie-sata`. Measured, on the same slice, sweeping the cell
#: size and changing nothing else:
#:
#: ===========  =============  ==============
#: cell size    x grid lines   Zdiff read
#: ===========  =============  ==============
#: 50 um        47             **12.2 ohm**
#: 45 um        61             **48.2 ohm**
#: 40 um        64             74.5 ohm
#: 35 um        72             74.5 ohm
#: 25 um        112            78.9 ohm
#: ===========  =============  ==============
#:
#: The answer is stable from 40 um down and collapses above it, and every one of
#: those runs decayed to -70 dB and exited zero. That is the failure mode phase 0
#: named -- a confident wrong answer at a zero exit code -- arriving through the
#: mesh rather than through the export, and a fixed cell size cannot catch it
#: because whether 50 um is fine depends on the geometry it is meshing.
#:
#: Six across the trace and five across the gap puts the cliff (4.1 cells across
#: the trace, 3.3 across the gap) a comfortable distance below the default rather
#: than just outside it.
CELLS_ACROSS_TRACE = 6.0
CELLS_ACROSS_GAP = 5.0


def grid_optimal_um(sliced: Slice, settings: ResolvedSimulation) -> float:
    """The cell size this slice actually needs, in micrometres.

    Never *coarser* than the setting, so a design that asks for a fine mesh gets
    one; finer whenever the geometry demands it. Rounded down to a tenth of a
    micrometre so the number in ``simulation.json`` -- and therefore the slice
    digest, and therefore the cache -- is a function of the geometry alone.
    """
    widths = [port.width_mm for port in sliced.ports if port.width_mm > 0]
    if not widths:
        return settings.grid_optimal_um
    narrowest = min(widths)
    needed = [settings.grid_optimal_um, narrowest * 1000.0 / CELLS_ACROSS_TRACE]
    gap = sliced.pair_gap_mm
    if gap:
        needed.append(gap * 1000.0 / CELLS_ACROSS_GAP)
    return math.floor(min(needed) * 10) / 10


def max_steps(sliced: Slice, settings: ResolvedSimulation) -> int:
    """The step limit, scaled to hold the simulated *duration* constant.

    ``max_steps`` bounds a run that never decays -- a slice with two plane layers
    and an artificial boundary is a resonant cavity, and some of them ring forever.
    What it is really bounding is simulated time, and an FDTD timestep is set by
    the cell size: halve the cell and the same physical settling needs twice the
    steps. Leaving the limit fixed while :func:`grid_optimal_um` refines the mesh
    therefore does not make a run cheaper, it makes it *stop earlier*, and a run
    stopped early comes back with energy still bouncing around -- which is visible
    as ``|Sdd21|`` above unity and, on `examples/pcie-sata`'s transmit pair,
    reached 1.23 where the same slice at the coarse mesh reached 1.06.

    So the limit follows the mesh. A design that names its own ``max_steps`` still
    gets what it asked for at the cell size it asked for.
    """
    cell = grid_optimal_um(sliced, settings)
    if cell <= 0 or cell >= settings.grid_optimal_um:
        return settings.max_steps
    return int(settings.max_steps * settings.grid_optimal_um / cell)


def netinfo_json(sliced: Slice) -> dict[str, Any]:
    """The nets under test.

    Optional upstream: without it every net but ``GND`` is meshed finely, which on a
    slice carrying a neighbour's track means spending cells on copper nobody asked
    about. aipcb knows exactly which two conductors matter, so it says so.
    """
    return {"nets": [{"name": name} for name in sliced.pair.nets]}


def write_inputs(work: Any, sliced: Slice, stackup: Stackup, settings: ResolvedSimulation) -> None:
    """Write ``fab/stackup.json``, ``simulation.json`` and ``netinfo.json``."""
    fab = work / "fab"
    fab.mkdir(parents=True, exist_ok=True)
    (fab / "stackup.json").write_text(
        json.dumps(stackup_json(stackup), indent=2) + "\n", encoding="utf-8"
    )
    (work / "simulation.json").write_text(
        json.dumps(simulation_json(sliced, settings), indent=2) + "\n", encoding="utf-8"
    )
    (work / "netinfo.json").write_text(
        json.dumps(netinfo_json(sliced), indent=2) + "\n", encoding="utf-8"
    )

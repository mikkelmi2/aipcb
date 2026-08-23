# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""What a signal-integrity simulation is allowed to assume.

Everything here is optional and nothing here reaches the board. A design that says
nothing about simulation simulates on the defaults below; the block exists so that
a design *can* say what it knows -- the link's real top frequency, how much board
around a pair is worth solving for, how tight a verdict should be -- rather than
inheriting a number somebody chose for a different board.

The units are spelled into the field names, following the convention M11 settled on
(``max_skew_mm``, ``impedance_diff_ohm``): a simulation frequency written as a bare
number is the one place a factor of a thousand hides best.
"""

from __future__ import annotations

from pydantic import Field

from aipcb.model.common import Ident, Strict

__all__ = ["ClassSimulation", "SimulationSettings"]


class ClassSimulation(Strict):
    """Per-net-class overrides. Every field falls back to the global default."""

    stop_ghz: float | None = Field(
        default=None,
        gt=0,
        description="Top of the swept band, in GHz. PCIe Gen3 wants 8, SATA III 6.",
    )
    start_mhz: float | None = Field(
        default=None, gt=0, description="Bottom of the swept band, in MHz."
    )
    margin_mm: float | None = Field(
        default=None, gt=0, description="Board kept around the pair's corridor."
    )
    launch_mm: float | None = Field(
        default=None,
        gt=0,
        description="Length of the axis-aligned launch each port feeds through.",
    )
    reason: str | None = None


class SimulationSettings(Strict):
    """Defaults for every pair, and the per-class overrides on top of them."""

    stop_ghz: float = Field(
        default=8.0,
        gt=0,
        description="Top of the swept band, in GHz, for classes that name none.",
    )
    start_mhz: float = Field(default=100.0, gt=0)
    margin_mm: float = Field(
        default=1.5,
        gt=0,
        description="How much board to keep around the pair's bounding corridor. "
        "The slice is cut here, so this is the neighbouring copper the solver sees.",
    )
    launch_mm: float = Field(
        default=1.5,
        gt=0,
        description="Length of the straight, axis-aligned launch added at each end "
        "of the pair so a port has somewhere to feed from.",
    )
    grid_optimal_um: float = Field(
        default=50.0,
        gt=0,
        description="Target cell size near copper, in micrometres. M12 measured the "
        "convergence of this on `examples/mcu-4layer`; 50 um moves the answer by "
        "under a percent against 25 um and costs a fifth of the time.",
    )
    grid_inter_layers: int = Field(
        default=8,
        ge=2,
        description="Cells stacked through each dielectric.",
    )
    max_steps: int = Field(
        default=40000,
        gt=0,
        description="Hard stop for the FDTD run. openEMS normally stops earlier, on "
        "energy decay; a board with a plane cavity in it may never reach that, and "
        "this is what bounds the run when it does not.",
    )
    timeout_s: int = Field(
        default=7200,
        gt=0,
        description="Wall-clock seconds one pair may take before the run is killed "
        "and its container reaped. This is a budget, not an estimate: a run that "
        "hits it produces no result at all, so it is sized above the slowest link "
        "on the board rather than near the typical one.",
    )
    impedance_tolerance: float = Field(
        default=0.10,
        gt=0,
        lt=1,
        description="Fractional distance from the class target that still passes.",
    )
    return_loss_db: float = Field(
        default=-10.0,
        lt=0,
        description="Worst differential return loss that still passes, in dB.",
    )
    insertion_loss_db: float = Field(
        default=-3.0,
        lt=0,
        description="Worst differential insertion loss that still passes, in dB.",
    )
    mode_conversion_db: float = Field(
        default=-20.0,
        lt=0,
        description="Worst differential-to-common conversion that still passes, in "
        "dB. This is the metric intra-pair skew shows up in: a length mismatch "
        "inside a pair turns differential signal into common-mode signal, which is "
        "what radiates.",
    )
    classes: dict[Ident, ClassSimulation] = Field(
        default_factory=dict,
        description="Per-net-class overrides, keyed by net-class name.",
    )
    reason: str | None = None

    def for_class(self, name: str) -> ResolvedSimulation:
        """The settings one net class actually runs on."""
        over = self.classes.get(name)
        return ResolvedSimulation(
            start_hz=(over.start_mhz if over and over.start_mhz else self.start_mhz) * 1e6,
            stop_hz=(over.stop_ghz if over and over.stop_ghz else self.stop_ghz) * 1e9,
            margin_mm=(over.margin_mm if over and over.margin_mm else self.margin_mm),
            launch_mm=(over.launch_mm if over and over.launch_mm else self.launch_mm),
            grid_optimal_um=self.grid_optimal_um,
            grid_inter_layers=self.grid_inter_layers,
            max_steps=self.max_steps,
            timeout_s=self.timeout_s,
            impedance_tolerance=self.impedance_tolerance,
            return_loss_db=self.return_loss_db,
            insertion_loss_db=self.insertion_loss_db,
            mode_conversion_db=self.mode_conversion_db,
        )


class ResolvedSimulation(Strict):
    """One net class's settings, with every fallback already taken."""

    start_hz: float
    stop_hz: float
    margin_mm: float
    launch_mm: float
    grid_optimal_um: float
    grid_inter_layers: int
    max_steps: int
    timeout_s: int
    impedance_tolerance: float
    return_loss_db: float
    insertion_loss_db: float
    mode_conversion_db: float

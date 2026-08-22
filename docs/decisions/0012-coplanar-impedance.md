# 0012 — Coplanar ground in the impedance derivation, and the mesh that has to resolve it

* **Status:** Accepted
* **Date:** 2026-08-22
* **Context:** milestone M13b/M13c, closing the finding
  [ADR 0011](0011-si-simulation.md) and [`m12.md`](../reports/m12.md) named as the
  most consequential of the M10–M12 chain. Builds on
  [ADR 0010](0010-highspeed.md) (the IPC-2141 derivation M11 shipped) and
  [ADR 0009](0009-pours.md) (the pours that turn out to be part of the geometry).

## Context

M11 derives a controlled-impedance pair's width from its class's
`impedance_diff_ohm` and the declared stackup, using IPC-2141's surface-microstrip
approximation with the standard edge-coupling factor. M12 then simulated every pair
on `examples/pcie-sata` and **every one came back below its declared target** —
`REFCLKP/N` at 50.9 Ω against 85, `SATA3_TXP/N` at 62.5 Ω against 100.

M12 diagnosed it correctly: *both* closed forms this project carries describe a
**bare** microstrip — a trace over a plane with nothing beside it — and every board
this toolchain builds pours ground up to its pairs at the class clearance. A trace
with ground beside it as well as under it is a coplanar waveguide with ground, and
its impedance is lower. The pour was a DRC number; it is really a geometry input.

One correction to the chain report while we are here, because it points the wrong
way and someone will act on it. `m12.md` and `docs/roadmap.md` both say the derived
widths are "systematically **narrow** for the boards it actually builds". They are
systematically **wide**. Impedance falls as a trace widens; a board reading *below*
its target was built too wide, and correcting the model has to narrow it. Measured
on `examples/pcie-sata`: the 85 Ω classes go from 0.2888 mm to 0.1846 mm and the
100 Ω class from 0.2390 mm to 0.1379 mm.

## Decision 1 — the model is IPC-2141's own coupling term, superposed per neighbour

`aipcb.impedance` gains a coplanar model, and it is **not** the textbook
conductor-backed-CPW conformal map. It is the closed form this project already
derives and audits with, applied once per neighbouring conductor and superposed as
partial capacitances.

The construction, in three lines. `coupling_factor(s, h) = 1 − c` with
`c = 0.48 exp(−0.96 s/h)` is IPC-2141's edge-coupled odd-mode factor, and since
`Z ∝ 1/C` a neighbour at gap `s` has added `x(s) = c / (1 − c)` times the trace's
own capacitance to its reference plane. Written that way it *superposes* — two
neighbours add `x₁ + x₂`, which is the partial-capacitance decomposition that
conformal-mapping treatments of coplanar lines are built on (Ghione & Naldi, IEEE
Trans. MTT-35, 1987, §II; Wadell §3.3). A differential pair with ground poured
alongside has two neighbours per trace: the other half at the pair gap, and the
pour at the pour gap. So

```
Zdiff_cpwg = Zdiff_microstrip × (1 + x(pair_gap)) / (1 + x(pair_gap) + x(pour_gap))
```

Two properties earn it the decision. It **reduces to exactly today's answer** when
nothing is poured alongside (`x(∞) = 0`), so no board without a pour beside its
pairs moves by a nanometre. And with one neighbour it *is* IPC-2141, so the
derivation and the audit still use one formula, which is the property ADR 0010
made the whole M11a design around.

### Why not the published conformal map

`grounded_cpw()` implements the Wadell/Simons/Ghione–Naldi conductor-backed CPW
closed form, with the complete elliptic integrals evaluated exactly by the
arithmetic-geometric mean. It is in the module and it is tested, because it is what
this decision was measured against and a claim nobody can check is not a
measurement. It is not what ships.

Across the twelve simulated links M12 left behind — the eleven on `pcie-sata` plus
the `mcu-4layer` USB pair its convergence sweep used — the mean and RMS error
against the solver is:

| model | mean | RMS | worst |
|---|---|---|---|
| bare microstrip, which is what M11 shipped | **+34.8 %** | 39.6 % | +67.0 % |
| Wadell/Simons conductor-backed CPW, odd mode | **+61.7 %** | 66.0 % | +107.6 % |
| the same, taken as a ratio against its own far limit | **+17.5 %** | 23.9 % | +47.0 % |
| **partial capacitances, which is what ships** | **+7.0 %** | **16.3 %** | +34.5 % |

The conformal form's trouble is its own far limit. As the coplanar gap opens, `k₁ →
0` and its effective permittivity tends to `εr` rather than to a microstrip's, so
it reads **109.6 Ω** for an isolated 0.2888 mm trace on 0.2104 mm of prepreg where
IPC-2141 reads **89.6 Ω**. Inside its domain — a genuine CPW, coplanar gap
comparable to the substrate — it is good: on the standard 50 Ω conductor-backed CPW
design (1.0 mm trace, 0.25 mm gap, 0.508 mm RO4350) it returns **48.4 Ω**, within
3 % of the intended 50. On geometries whose backing plane dominates, which is every
geometry this tool derives, the bias is larger than the coplanar effect being
measured. Dividing it out helps and does not fix it, because the numerator carries
the same bias in a different amount.

There is a second, smaller reason, and it is the same one CLAUDE.md keeps
re-learning about external premises. The published thickness correction for CPW
(`Δ = 1.25 t/π (1 + ln(4π w/t))`, Simons eq. 3.55) has domain `t ≪ s`. On the
geometries this tool derives `t/s` reaches 0.23 and the correction subtracts *half
the gap*: the same 85 Ω-class geometry reads 38 Ω with it and 106 Ω without. The
ratio construction has no thickness term to be out of domain, because thickness
lives entirely in the IPC-2141 baseline where it is in domain.

## Decision 2 — the model is chosen from the `pours:` block, and published

`highspeed.pour_gap_for()` reads the pour gap off the source: for the class's
layer, the nearest pour of another net, at the **larger** of that pour's clearance
and the class's own — because KiCad enforces the larger of two nets' rules, and the
width has to be derived before any copper exists.

Two things this deliberately does not do, stated rather than hidden. It does not
reason about where a `scope: region` pour actually reaches, because the pair's
route does not exist yet; on a board whose pour is a patch somewhere else the
derivation is conservative in the direction of a narrower trace. And it takes the
*declared* clearance rather than the filled board's measured gap, for the same
reason — though M11e already measures the latter and reports it.

Which model each class used is **published, not inferred**: `ImpedanceTarget.model`
is `microstrip` or `cpwg`, it reaches `check`'s JSON under
`summary.highspeed.classes`, and `hs-impedance-model` says it in prose with the
geometry and the pour gap beside it. A width is a number with a model behind it,
and before M13b the model was always bare microstrip on boards that were never bare
microstrips, with nothing in the output saying so.

## Decision 3 — the pour clearance is now an impedance input, so validation says so

`impedance-pour-gap-sensitive` warns when one standard etch tolerance on the
pour-to-track gap (`GAP_TOLERANCE_MM = 0.025`, one mil) moves the differential
impedance by more than a fraction of the target that the class may set with
`pour_gap_sensitivity` (default 0.05, half of `GEOMETRY_TOLERANCE`).

Measured on `examples/pcie-sata`'s stackup, at the 85 Ω target: a 0.05 mm pour gap
is worth **6.3 %**, 0.075 mm **5.2 %**, 0.10 mm **4.4 %**, the board's actual
0.15 mm **3.1 %**, and 0.30 mm **1.3 %**. So the shipped board does not trip it,
and a design that pours much tighter does. A class that holds its target only at
exactly the nominal gap has spent part of its budget on a fabrication tolerance
before the trace is drawn, and that is worth knowing before the board is made
rather than after.

## Decision 4 — the mesh has to follow the geometry, and this is a correctness fix

**This is the sharpest thing M13b found and it is not about impedance at all.**

M12 calibrated a fixed 50 µm cell on `examples/mcu-4layer`'s 0.25 mm pair at a
0.2 mm gap, measured its convergence across a 4.5× change in density, and shipped
it as the default. The coplanar model derives a 0.185 mm pair at a 0.15 mm gap, and
on that geometry the same 50 µm mesh is **silently catastrophic**. Sweeping the
cell size on one slice and changing nothing else:

| cell | x grid lines | Zdiff read | energy decay |
|---|---|---|---|
| 50 µm | 47 | **12.2 Ω** | −73 dB |
| 45 µm | 61 | **48.2 Ω** | −61 dB |
| 40 µm | 64 | 74.5 Ω | −61 dB |
| 35 µm | 72 | 74.5 Ω | −64 dB |
| 30 µm | — | 76.7 Ω | −59 dB |
| 25 µm | 112 | 78.9 Ω | — |

Every one of those runs decayed, exited zero and reported no warning. The 50 µm
answer is wrong by a factor of seven and looks exactly as trustworthy as the 25 µm
one. That is precisely the failure mode ADR 0011 phase 0 named — *a confident wrong
answer at a zero exit code* — arriving through the mesh rather than through the
export, and a fixed cell size cannot catch it, because whether 50 µm is fine depends
on the geometry being meshed.

So `si.inputs.grid_optimal_um()` derives the cell from the slice: never coarser
than the setting, and fine enough for **6 cells across the trace and 5 across the
gap** (`CELLS_ACROSS_TRACE`, `CELLS_ACROSS_GAP`). The cliff above is at 4.1 cells
across the trace and 3.3 across the gap, so the rule sits a clear distance below it
rather than just outside it. On `pcie-sata` that is 30.7 µm for the PCIe classes and
22.9 µm for SATA.

It refines `mcu-4layer` too, to **41.6 µm** rather than the declared 50 — the board
M12 calibrated on is wide-geometry but not wide enough to be left alone. That is
inside the 25–100 µm band M12's own convergence sweep covered on that exact pair,
over which the answer moved 5.3 %, so the calibration still bounds what runs. It is
not the same as the default being untouched, and saying "it leaves `mcu-4layer`
alone" would have been wrong; the number is asserted in
`tests/test_simulation.py::TestSolverInputs` so the claim cannot drift.

`max_steps` follows the mesh for the same reason (`si.inputs.max_steps()`). An FDTD
timestep is set by the cell, so a finer mesh needs proportionally more steps to
simulate the same physical settling time. Leaving the limit fixed does not make a
run cheaper — it makes it *stop earlier*, and a run stopped early comes back with
energy still bouncing around, visible as `|Sdd21|` above unity.

**The consequence is cost, and it is not where one would guess.** Measured rather
than reasoned: `pcie-sata`'s SATA slice at 22.9 µm meshes to **3.63 M cells**
against M12's 3.41 M at 50 µm — barely more, because gerber2ems refines only near
copper and the slice is mostly empty. What moves is the step count, 40 000 → 87 336,
because the *smallest* cell sets the timestep. So the cost roughly doubles rather
than multiplying by the square of the refinement, which is the opposite of the
intuition and is why it was measured. On the PCIe slices, which are denser relative
to their area, cells did roughly double (0.52 M → 0.99 M) and the wall clock went
0.9 → 2.1 minutes for the transmit link and 2.2 → 7.9 for the reference clock.

That is the price of the number being right, and it is recorded here rather than
traded away: the alternative is a default that returns 12 Ω for an 85 Ω line without
complaining.

## Decision 5 — the skew verdict is a fit across frequency, and it stays a warning

M12 asked whether simulation discriminates the two pairs M11 delivered over their
skew budget. The answer through the published scalar was **no, and worse than no**:
the three links `mode_conversion` flagged were the three *best*-matched pairs on the
board, and both mismatched ones passed. The cause was not that the physics is
invisible — `REFCLKP/N` tracks `|sin(π f Δτ)|` to within 3.2 dB across its band — it
was that a worst-in-band maximum reads the **floor**, and the floor varied by more
than 25 dB between pairs on one board while the skew moved it by about 3 dB.

`si.results.fit_skew()` fits the two-term model

```
|Scd21|² = (|Sdd21|² + |Scd21|²) · sin²(π f Δτ) + floor²
```

over the band. The amplitude is measured rather than fitted — a pair with skew Δτ
transmits `cos(π f Δτ)` differentially and converts `sin(π f Δτ)`, so the two
together are what the line delivered; letting the fit choose it lets a lossy link
and a big skew trade against each other. For any candidate Δτ the best flat floor
is a closed form (the mean of what the skew term does not account for, in power),
so what is left is a one-dimensional scan. Coarse then fine, on a fixed grid,
bounded at `1/(2 f_max)` where the model turns over inside the band and a longer
delay stops being distinguishable from a shorter one. No seed, no tolerance, the
same answer on any machine.

Three verdicts, and the third is the one a scalar could not express:

* **pass** — the fitted delay, converted to a length with the pair's *own* measured
  ps/mm, is inside the class's `max_skew_mm`;
* **warn** — it is outside;
* **under-floor** — the skew term never rises far enough above the fitted floor for
  the delay to be a measurement, and the number returned is an **upper bound**.

`mode_conversion` stays, because it is a real and reproducible measurement of how
much differential energy leaves as common mode. It stops claiming to be a skew
verdict: its verdict value is now `warn-low-confidence` and the finding says so in
its first line.

**Promotion to error stays out of scope**, and the reason is a measurement rather
than caution: the fit's false-positive behaviour has to be characterised across a
full board first. `docs/reports/m13.md` carries what has been measured so far.

## Consequences

* Every controlled-impedance class on a board that pours ground beside it derives a
  narrower trace than before M13b. On `examples/pcie-sata` that is the only board;
  the other ten declare no `impedance_diff_ohm` and none of their geometry moves.
* A narrower pair inverted an assumption `route/pairs.py` had been relying on —
  that a class's single-ended `trace_width_mm` is *narrower* than its pair's, which
  every board here declared and none of them stated. Two consequences, both fixed:
  the coupled run's hand-over point is now checked against the fan-out's own free
  space as well as its own, and a fan-out is never drawn wider than the pair it
  feeds.
* `simulate` is materially more expensive on fine geometries, by design.
* Nothing here changes what a fabricator receives on a board without a pour beside
  its controlled-impedance pairs.
* **Re-measure at each KiCad major and at each gerber2ems bump**, in the spirit of
  ADR 0009's Finding 1: the mesh cliff above is a property of *this* mesh generator
  at *this* commit, and the cell-count rule is calibrated against it.

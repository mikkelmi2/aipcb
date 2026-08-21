# Upgrade: signal-integrity simulation integration — openEMS/gerber2ems (M12)

Close the gap M11e explicitly left open: go from rule-based verification to actual electromagnetic simulation of the controlled-impedance pairs, using the open source openEMS (FDTD field solver) driven by Antmicro's gerber2ems, and translate the results into the same structured, source-referenced feedback as every other check. The target boards are the existing examples, with pcie-sata as the flagship: simulate its routed pairs and report differential impedance, insertion/return loss against targets.

Simulation is expensive, but **Phase 0 measured how expensive and the answer was much better than this spec originally assumed**: one pair takes **30 s – 2 min** at default settings (105 k–653 k cells, 16 cores), not the "minutes to hours" first written here. A small board's full pair set is therefore *minutes*, not an overnight job.

Design for that measured reality: `simulate` stays a **separate explicit command and does not become part of `check`** (its runtime is still far above the interactive loop's, and its results are engineering judgement rather than a gate), but do not design around overnight/CI-only economics. Per-pair granularity and caching still matter — they are the difference between 30 s and 0 s on an unchanged pair.

Phase 0 also validated the solver itself, so accuracy is not the open question: the toolchain reproduced Antmicro's VNA-validated `stub_short` case to within **1.1 % at 500 MHz and 1.7 % at the 5 GHz stub resonance**.

## Session context (this runs in a fresh session)

Read `docs/reports/m11.md` first — especially which pcie-sata pairs routed and which were handed over (only routed pairs can be simulated), and the stackup decisions from the M11 ADR. Also read `docs/reports/m10.md` for the fill mechanics (simulation consumes filled boards) and the relevant ADRs. If an expected report is missing (reports before M10 predate the report requirement), reconstruct what's needed from `docs/decisions/`, the git log, and the code itself; note the gap in your own report rather than stopping.

## Phase 0 — Environment verification — ✅ **COMPLETE, PASSED. Do not redo it.**

Phase 0 ran ahead of this milestone and passed. Its findings are in
[`docs/decisions/0011-si-simulation.md`](../decisions/0011-si-simulation.md) — read
that instead of repeating the work. The headlines you need:

- The toolchain builds and works; the pin (gerber2ems `9eaf3033`, openEMS
  `a3058772`, image `sha256:e1921ec2…`) is recorded in the ADR. Antmicro publishes
  no prebuilt image — the "container route" is a from-source build via their
  Dockerfile, ~7 min. `podman` is installed on this machine.
- Single-pair timing: **30 s – 2 min**. Vendor VNA case reproduced to 1.1–1.7 %.
- The **export-gap list is the most valuable output** — read it in full. Two of its
  gaps failed *silently*, and both are now fixed ahead of this milestone:
  `aux_axis_origin` is emitted (without it gerber2ems dropped every via), and the
  position file matches the consumer's `*pos.csv` glob (without which it was never
  read). Remaining gaps — `stackup.json`, drill filename flag, simulation ports,
  `netinfo.json`, and the fact that `aipcb export` does not route — are M12a's work.
- **Two findings that change your plan, from the ADR:** impedance numbers are not
  yet trustworthy on `examples/diff-pair` (three port/mesh configs of the same
  geometry gave ≈26 Ω, ≈340 Ω, ≈950 Ω, with a non-physical |Sdd21| > 1 on the coarse
  run) because its nearest copper is 1.51 mm away while every gerber2ems example
  sits 0.12 mm above its plane — **calibrate on `examples/mcu-4layer` instead**
  (GND 0.48 mm below F.Cu). And **`examples/diff-pair` cannot reach its declared
  100 Ω** — Hammerstad puts that geometry at ~270 Ω differential, so the acceptance
  criterion below about diff-pair "within plausible range of its design target" is
  **not satisfiable as written**; the example's target is wrong, and reporting that
  is the correct outcome, not a failure to engineer around.

Antmicro's `kicad-si-simulation-wrapper` was **not** evaluated in Phase 0, so
M12a's reuse-vs-reimplement call is still fully open and yours to make.

<details>
<summary>The original Phase 0 instructions, for reference</summary>

The environment risk is the biggest unknown in this milestone; retire it first and record everything in `docs/decisions/000x-si-simulation.md`:

1. Install the openEMS + gerber2ems toolchain. Prefer the container route from the gerber2ems repository over building from source; if building, use the specific openEMS commit the gerber2ems README recommends — they test against a pinned version. Network/domain constraints may bite here: if required resources are unreachable, stop and report exactly what's needed rather than improvising
2. Run gerber2ems's own bundled examples (they include VNA-validated cases) and confirm outputs are produced and plausible
3. Feed it a *simple aipcb-exported board* (e.g. the diff-pair example): export Gerbers + drill + stackup, run a simulation on one pair. This is the make-or-break compatibility test — gerber2ems expects specific stackup-description input; document the exact format it needs and any gaps in what `aipcb export` produces
4. Measure wall-clock time for one pair simulation at default settings; this calibrates all planning below
5. Only proceed to M12a when phase 0 works end-to-end. If it fundamentally cannot (toolchain broken, format incompatibility too deep), stop and present findings — a working phase 0 in a container that aipcb shells out to is an acceptable fallback architecture

</details>

## M12a — Slice generation

Full-board FDTD is computationally hopeless; simulate per pair using the slice approach (study Antmicro's kicad-si-simulation-wrapper for the method — it may be usable directly since it targets KiCad 9; evaluate reuse vs. reimplement and record the decision):

- `aipcb simulate --net-class pcie_pair` (or `--net PCIE_TX`) generates, per pair: a board slice containing the pair, neighboring nets, and all vias/planes in the region; series passives in the signal path (AC-coupling caps, 0 Ω) replaced by shorts — aipcb *knows* these from `role: ac_coupling`, an advantage over geometric-only slicing
- Slice extents from a configurable margin around the pair's bounding corridor
- Port placement at the pair's endpoints (pads/finger terminals), differential excitation
- Everything derived from source + built board deterministically; slices are reproducible artifacts in `out/si/<net>/`

## M12b — Simulation orchestration

- `aipcb simulate` runs gerber2ems/openEMS per slice. **Sequential by default; `--parallel N` optional — and given the measured 30 s – 2 min per pair, treat parallelism as genuinely optional.** A single openEMS run already uses the machine's cores, so process-level parallelism may buy little and cost complexity. Make the call yourself, measure it, and record the decision and the numbers behind it; "sequential only, because measurement showed N-way added nothing" is a perfectly good answer
- Simulation parameters (frequency range, mesh oversampling, boundary) in a source-level block with sensible defaults per net class (PCIe Gen3: DC–8 GHz; SATA III: DC–6 GHz); overridable
- **Caching stays**, and is the highest-value part of this section: hash of (slice geometry + parameters) → skip unchanged pairs on re-run; `--force` to override. Even at the measured timings it is the difference between 30 s and 0 s per unchanged pair, which is what makes an edit-resimulate loop tolerable
- Progress and per-pair timing to the console; a run manifest (what ran, what was cached, durations) saved alongside results
- Failures are per-pair, not global: one diverging simulation must not kill the batch; report it and continue

## M12c — Structured results

Translate raw output (S-parameters, impedance profiles) into the established feedback format:

- Per pair: differential impedance vs. target (from the net class), with worst-case deviation and its location along the trace where derivable; insertion loss and return loss at the class's key frequencies; pass/warn per metric against configurable thresholds (defaults: impedance ±10 % warn, return loss > −10 dB warn — document the rationale, and mark thresholds explicitly as engineering defaults, not standards compliance)
- Output: human summary + `aipcb simulate --json`, each finding pointing at the pair's source lines — same contract as `check`
- Keep the raw S-parameter files (touchstone) in `out/si/` for engineers who want them in scikit-rf or elsewhere; consider scikit-rf for the post-processing implementation itself
- Explicitly documented, same honesty as M11e: simulation accuracy depends on stackup data matching what the fab actually builds; this validates the *layout*, it does not replace fab impedance coupons or hardware measurement

## Acceptance

- ~~Phase 0 findings recorded, including the compatibility verdict and single-pair timing~~ — **already satisfied**, see ADR 0011
- **Revised by Phase 0:** one pair simulated end-to-end on **`examples/mcu-4layer`** (not diff-pair — see Phase 0 above for why diff-pair is the wrong calibration target), results within a plausible sanity band of its design target — not tight tolerance; document the band chosen and why. Separately, *report* that `examples/diff-pair`'s declared 100 Ω target is unreachable for its geometry rather than trying to satisfy it
- pcie-sata: all *routed* pairs simulated; the structured report renders with per-pair metrics; at least the impedance-vs-target comparison works against the M11a-declared targets; handed-over (unrouted) pairs are listed as not-simulated, not silently absent
- Caching test: second run with no changes skips all pairs; a source change to one pair re-simulates only that pair
- A deliberately degraded case (e.g. a pair re-routed over a plane split, or with an undersized width override) shows measurably worse results than the clean version — proving the simulation actually discriminates
- Determinism where it applies: slice generation byte-stable; simulation numerics are only required to be stable enough that pass/warn verdicts are reproducible across runs on the same machine
- All prior examples and tests unaffected

## Out of scope (record in `docs/roadmap.md`)

- Crosstalk multi-pair simulation, eye diagrams, IBIS driver models — note as future candidates
- Automatic design fixes from simulation results (the agent loop does that by reading the JSON)
- Thermal or power-integrity simulation
- Making simulation part of `check` — it stays a separate explicit command due to runtime

## Guardrails

- No changes to router, stretcher, or build/check semantics; M12 is downstream consumption of exported boards plus a new command
- The external toolchain is pinned (container digest or commit hash recorded in the ADR and in a lockfile the repo carries); upgrades are deliberate, not accidental
- If phase 0 reveals the integration needs aipcb export changes (e.g. stackup file format), those changes go through the normal schema/ADR process and must keep all existing outputs byte-stable

## Delivery report (required, in-repo)

Write `docs/reports/m12.md`: what was built and verified (with numbers), the phase-0 environment findings, per-pair simulation results for pcie-sata with the impedance-vs-target table, decisions and why (including the wrapper reuse-vs-reimplement call), defects found and how they surfaced, anything deliberately not built, and open questions. Include the performance table: per-pair simulation wall-clock times, cache hit behavior, and total batch time for pcie-sata — this stage's economics decide how it fits CI, so measure them. Measured claims over asserted ones.

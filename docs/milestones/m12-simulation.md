# Upgrade: signal-integrity simulation integration — openEMS/gerber2ems (M12)

Close the gap M11e explicitly left open: go from rule-based verification to actual electromagnetic simulation of the controlled-impedance pairs, using the open source openEMS (FDTD field solver) driven by Antmicro's gerber2ems, and translate the results into the same structured, source-referenced feedback as every other check. The target boards are the existing examples, with pcie-sata as the flagship: simulate its routed pairs and report differential impedance, insertion/return loss against targets.

Simulation is slow by nature (minutes to hours per pair) — this is a batch/CI stage, not part of the interactive loop. Design everything around that: explicit invocation, per-pair granularity, cached results.

## Session context (this runs in a fresh session)

Read `docs/reports/m11.md` first — especially which pcie-sata pairs routed and which were handed over (only routed pairs can be simulated), and the stackup decisions from the M11 ADR. Also read `docs/reports/m10.md` for the fill mechanics (simulation consumes filled boards) and the relevant ADRs. If an expected report is missing (reports before M10 predate the report requirement), reconstruct what's needed from `docs/decisions/`, the git log, and the code itself; note the gap in your own report rather than stopping.

## Phase 0 — Environment verification (do this before any aipcb code)

The environment risk is the biggest unknown in this milestone; retire it first and record everything in `docs/decisions/000x-si-simulation.md`:

1. Install the openEMS + gerber2ems toolchain. Prefer the container route from the gerber2ems repository over building from source; if building, use the specific openEMS commit the gerber2ems README recommends — they test against a pinned version. Network/domain constraints may bite here: if required resources are unreachable, stop and report exactly what's needed rather than improvising
2. Run gerber2ems's own bundled examples (they include VNA-validated cases) and confirm outputs are produced and plausible
3. Feed it a *simple aipcb-exported board* (e.g. the diff-pair example): export Gerbers + drill + stackup, run a simulation on one pair. This is the make-or-break compatibility test — gerber2ems expects specific stackup-description input; document the exact format it needs and any gaps in what `aipcb export` produces
4. Measure wall-clock time for one pair simulation at default settings; this calibrates all planning below
5. Only proceed to M12a when phase 0 works end-to-end. If it fundamentally cannot (toolchain broken, format incompatibility too deep), stop and present findings — a working phase 0 in a container that aipcb shells out to is an acceptable fallback architecture

## M12a — Slice generation

Full-board FDTD is computationally hopeless; simulate per pair using the slice approach (study Antmicro's kicad-si-simulation-wrapper for the method — it may be usable directly since it targets KiCad 9; evaluate reuse vs. reimplement and record the decision):

- `aipcb simulate --net-class pcie_pair` (or `--net PCIE_TX`) generates, per pair: a board slice containing the pair, neighboring nets, and all vias/planes in the region; series passives in the signal path (AC-coupling caps, 0 Ω) replaced by shorts — aipcb *knows* these from `role: ac_coupling`, an advantage over geometric-only slicing
- Slice extents from a configurable margin around the pair's bounding corridor
- Port placement at the pair's endpoints (pads/finger terminals), differential excitation
- Everything derived from source + built board deterministically; slices are reproducible artifacts in `out/si/<net>/`

## M12b — Simulation orchestration

- `aipbc simulate` runs gerber2ems/openEMS per slice: sequential by default, `--parallel N` optional
- Simulation parameters (frequency range, mesh oversampling, boundary) in a source-level block with sensible defaults per net class (PCIe Gen3: DC–8 GHz; SATA III: DC–6 GHz); overridable
- **Caching**: hash of (slice geometry + parameters) → skip unchanged pairs on re-run; `--force` to override. This is what makes iteration tolerable
- Progress and per-pair timing to the console; a run manifest (what ran, what was cached, durations) saved alongside results
- Failures are per-pair, not global: one diverging simulation must not kill the batch; report it and continue

## M12c — Structured results

Translate raw output (S-parameters, impedance profiles) into the established feedback format:

- Per pair: differential impedance vs. target (from the net class), with worst-case deviation and its location along the trace where derivable; insertion loss and return loss at the class's key frequencies; pass/warn per metric against configurable thresholds (defaults: impedance ±10 % warn, return loss > −10 dB warn — document the rationale, and mark thresholds explicitly as engineering defaults, not standards compliance)
- Output: human summary + `aipcb simulate --json`, each finding pointing at the pair's source lines — same contract as `check`
- Keep the raw S-parameter files (touchstone) in `out/si/` for engineers who want them in scikit-rf or elsewhere; consider scikit-rf for the post-processing implementation itself
- Explicitly documented, same honesty as M11e: simulation accuracy depends on stackup data matching what the fab actually builds; this validates the *layout*, it does not replace fab impedance coupons or hardware measurement

## Acceptance

- Phase 0 findings recorded, including the compatibility verdict and single-pair timing
- diff-pair example: one pair simulated end-to-end, results within plausible range of its design target (sanity band, not tight tolerance — document the band chosen and why)
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

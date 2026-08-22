# M10 → M11 → M12: chain report

Unattended orchestrator run, 2026-08-21 to 2026-08-22, from
[`docs/milestones/orchestrator-m10-m12.md`](../milestones/orchestrator-m10-m12.md).
Gate-by-gate detail is in [`orchestrator-log.md`](orchestrator-log.md); this is the
executive view.

**All three milestones are delivered.** The chain halted once, honestly, on M10's
own guardrail, and resumed after the project owner decided the blocking question.
Two subagents were killed mid-milestone by infrastructure failures; both were
recovered by a single scoped remediation pass each, and both recoveries are visible
in the reports rather than smoothed over. Every gate was re-run by the orchestrator
rather than taken from a subagent's word.

## Status per milestone

**M10 — copper pours, stitching vias & plane integrity: delivered.** It stopped
first. `kicad-cli` 9.0.8 has no way to fill a zone, and the milestone's own
guardrail said to stop rather than work around it — correctly, because an unfilled
pour exports as *no copper at all, silently*, while DRC over it reports a clean
board. The owner accepted [ADR 0009](../decisions/0009-pours.md) Option 1: drive
KiCad's own filler through a `pcbnew` subprocess with a version lock. Delivered on
that basis: 8 of 10 examples poured (the two without a ground net deliberately have
none, and are the live backward-compatibility witness), **0 DRC violations on every
filled board**, `filled_polygon` count **0** in every build output — the stability
policy as a property of the file rather than a promise. Fill costs 0.70 s/board;
stitching 0.11 s for all ten examples; plane analysis 36 ms.
[`m10.md`](m10.md).

**M11 — controlled impedance, card edges, via transitions & verification:
delivered, with one acceptance clause explicitly unmet.** The `pcie-sata` reference
board routes **90/90 connections, 11 of 11 pairs coupled, nothing handed over** —
better than the milestone's own bar of "2 of 11 finished manually is success", but
it means the reference board never exercises the hand-over path. Reference
continuity and via stubs are clean: 629.3 mm projected, **0** plane crossings,
worst stub **0.000 mm** against a 0.5 mm threshold. The unmet clause is skew, below.
`route/stretch.py` was never touched — the three M11d rules landed in
`route/pairs.py`, gated on `impedance_diff_ohm`, which is stricter than the
guardrail required. [`m11.md`](m11.md), [ADR 0010](../decisions/0010-highspeed.md).

**M12 — signal-integrity simulation: delivered.** `aipcb simulate` cuts a slice per
pair out of a routed board, runs it through openEMS via gerber2ems in a pinned
container, and turns S-parameters into the same source-referenced findings as every
other check. All **11 links** on `pcie-sata` simulated. Phase 0 had already retired
the environment risk, reproducing Antmicro's VNA-validated case to within 1.1–1.7 %.
[`m12.md`](m12.md), [ADR 0011](../decisions/0011-si-simulation.md).

## Headline numbers

| | |
|---|---|
| Milestones delivered | **3 of 3** |
| Test suite | 874 → **1090+**, green throughout; `ruff` clean; `mypy --strict` clean on 60 → **85** source files |
| Examples | 10 → **11**, all byte-stable, **0 DRC errors** on every one |
| Suite runtime | 10 m 58 s → **31 m 58 s** |
| Single-pair simulation | 54 s – ~15 min, by slice *area* |
| Full `pcie-sata` batch | ~2 h; a re-run with nothing changed costs **0.3 s** |

Pipeline cost stayed flat across the chain: from M10's baseline to M11, validate
+1.2 %, build +0.9 %, route +0.8 %, export +1.3 %, check −1.5 %, largest
per-example delta 0.14 s. M11d's re-tightening adds **0.139 s (0.35 %)** and M11e's
analysis **0.111 s (0.28 %)** on the densest board. Pours cost nothing at build
time — the whole point of the stability policy.

## What the chain learned about itself

Three times a milestone corrected a claim an earlier one had made, which is the
main argument that the reports are worth reading:

1. **M10 corrected its own fill-determinism finding.** The stop report called the
   fill byte-deterministic. Closer measurement: the fill *geometry* is
   byte-identical, but the filled *file* differs every run by exactly 12 lines —
   UUIDs KiCad's own writer adds. Hence the guarantee covers unfilled build output.
2. **M11 corrected the via-stub number** it had inherited, after finding `pcbnew`
   normalises through-via spans.
3. **M12 found that M11's derived trace widths are systematically wrong for the
   boards this tool actually builds.** Every simulated link comes back below its
   declared target — `REFCLK` at 50.9 Ω against 85 Ω, `SATA3_TX` at 62.5 Ω against
   100 Ω. Not the router's fault: every example pours ground up to its pairs, and
   **both closed-form models the width solver uses are bare microstrip**, which
   know nothing about coplanar ground. This is the single most consequential
   finding of the chain, and it exists only because simulation was built.

This is now project culture rather than luck: `CLAUDE.md` records the rule the run
kept re-learning — *ADR premises about external tools have expiry dates; measure
the tool you actually have, at the version you have.*

## The one question left open, and its answer

M11 delivered `REFCLKP/N` and `PCIE_RXP/N` at **0.292 mm** and **0.256 mm** of
intra-pair skew against the **0.125 mm** their class declares — annotated and
warned about, not handed over. That was flagged as the chain's closest call, and
M12 was asked whether simulation discriminates.

**It does not, and on the published verdict it does worse than not discriminating:**
both over-budget pairs come back `pass`, while all three `warn`s are pairs
comfortably inside budget (`SATA3_RX` at 0.004 mm reads −14.3 dB; `REFCLK` at
0.292 mm reads −23.0 dB). The cause is a mode-conversion floor varying by more than
25 dB across nominally identical links, in a quantity the skew moves by ~3 dB. Read
across frequency instead of as a scalar, `REFCLK` *does* show a clean skew
signature, tracking `|sin(πfΔτ)|` to within 3.2 dB — so the physics is visible even
where the verdict is not. M11's physical argument is corroborated; the acceptance
question still gets a "no".

## Every flagged deviation

**M10** — 8/10 examples poured; `thermal_pad` implemented as the explicit list the
spec permits rather than a role; the fragmenting case split between a synthetic
board and `qfn-fanout`; keepout zones built but exercised only by a purpose-built
test; **a new CI prerequisite — the `kicad` package, not just `kicad-cli`**;
`--no-route` checks a different board and now says so louder.

**M11** — the M11e report does not run clean on two pairs (above); **the
`diff-pair` copper sliver was not resolved**, and could not have been — M11d rule 1
only applies to classes naming an `impedance_diff_ohm`, and that class names none,
so the rule never ran on that board; `standoff_k`'s default of 3 refuses every pair
on the milestone's own reference board, which sets 1.4; four spec field names
renamed and `class: diff_pair` not implemented; an unremovable KiCad
`lib_footprint_mismatch` warning on every card-edge board; **PERST# and the A1–B17
presence-detect strap are absent from the netlist**, so "90/90 routed" omits two
signals a real card needs; no pair handed over, so M9f is untested on the shipped
example; AC capacitors validated but never placed; impedance derivation is
microstrip-only and an inner-layer class silently gets a microstrip width back.

**M12** — impedance sits systematically below every target (above); **eight
nominally identical `sata` links simulate 29 Ω apart**, and a real spread could not
be separated from an unconverged extraction; **11 of 22 insertion-loss figures are
positive** (truncation noise), so `insertion_loss=pass` means "no evidence of excess
loss", not a measurement; `mode_conversion_db` is not comparable across net classes;
the `.kicad_pcb` stackup disagrees with the source stackup, found and deliberately
not fixed because it would change every existing board; `aipcb export` now writes
two drill files where it wrote one; worst-case impedance deviation is reported but
not *located* along the trace; `--parallel` not built, measured rather than assumed;
**the "deliberately degraded case" acceptance item was not run** and was added to
the deviations table by the orchestrator rather than by the milestone.

**Process** — two subagents were killed by infrastructure failures (an API session
limit in M11, a 529 in M12), each after committing its code but before finishing
its report; each was recovered by one scoped remediation pass, and both reports say
so in their own headers.

## Open defects, carried forward

1. **`_repair` can route across another net** (`route/plan.py`). Found by M11 on
   PRSNT, unexplained, recorded in `docs/roadmap.md`. It does not manifest on any
   bundled example today. **This is a correctness bug in the router and the most
   serious open item in the chain.**
2. **`si/runner.py` leaks containers.** Cleanup runs only on
   `subprocess.TimeoutExpired`; when the client is killed outright the container
   survives. This happened twice in this run — a dead session's container was found
   still running sixteen cores, and a relaunch briefly had two containers writing
   one directory. A pre-flight check would cost nothing.
3. **The same pair is reported out of budget three times with two different
   numbers** — 0.251 from `_match_lengths`, 0.292 from `measure_skew`, again as
   `hs-skew` — with nothing explaining why they differ.

## Recommended next action

**Decide whether the two over-budget pairs are acceptable.** Simulation declined to
settle it, so it is an engineering judgement: ≈1.8–2.1 ps of skew against a 125 ps
Gen3 unit interval, on pairs whose class declares 0.125 mm and which were delivered
at 2.3×. `verify: error` already exists to promote skew from warning to error if the
answer is no.

Then, in rough order of value:

1. **Fix `_repair`** (open defect 1). It is latent today and unbounded tomorrow.
2. **Give the width solver a CPWG model.** Every controlled-impedance example on
   this tool is currently narrow for the board it ships on, and M12 measured how
   much. This is the finding with the longest reach.
3. **Add the container pre-flight check** (open defect 2) — minutes of work, and
   this run hit it twice.
4. **Run the degraded-case experiment M12 skipped**, so "does simulation
   discriminate a worse layout" has a measured answer rather than an inference from
   an accidental case.

Deferred deliberately and still worth scheduling: the pad-UUID aliasing fix (it
wants its own milestone, since it changes the UUID of every affected pad), the
`.kicad_pcb`-versus-source stackup disagreement, and pour-aware routing.

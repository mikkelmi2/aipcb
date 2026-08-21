# M10 → M11 → M12: chain report

Unattended orchestrator run, 2026-08-21, from
[`docs/milestones/orchestrator-m10-m12.md`](../milestones/orchestrator-m10-m12.md).
Gate-by-gate detail is in [`orchestrator-log.md`](orchestrator-log.md); this is the
executive view.

**The chain stopped at M10, on M10's own named stop-condition, and is waiting on a
decision that only the project owner can make.** One milestone of three ran to a
verdict; a second never started; the third's environment work completed in parallel
and passed. Nothing was worked around.

## Status per milestone

**M10 — copper pours, stitching vias & plane integrity: STOPPED, nothing
implemented.** The milestone's guardrail says to stop if KiCad's CLI cannot fill
zones headlessly, and it cannot: `kicad-cli` 9.0.8 exposes `{drc, export, render}`
with no fill or refill anywhere, which the orchestrator confirmed independently of
the subagent. The consequence is not cosmetic — an unfilled pour exports as *no
copper at all, silently* (`F.Cu` gerber: 596 bytes / 2 coordinate records unfilled
vs 1066 / 20 filled), and DRC over an unfilled zone reports the plane's own nets as
unconnected. Both halves of M10b therefore have no surface to stand on. What the
session did deliver is the work the spec demanded *before* code: the fill-surface
probe above, a fill-determinism measurement (**byte-identical across 8 runs on 2
boards**), a verified answer on per-pad-instance keying, and the ten-example
performance baseline. `docs/reports/m10.md`, [ADR 0009](../decisions/0009-pours.md)
(status *Proposed — blocked*).

**M11 — high-speed: NOT DISPATCHED.** M11e projects each controlled-impedance net
onto its declared reference plane *post-fill*. With no plane copper, that check has
nothing to look at, and the pcie-sata acceptance ("the M11e report runs clean")
could not be honestly evaluated. Starting it would have produced work no gate could
verify.

**M12 — SI simulation: PHASE 0 ONLY, PASS.** Run in parallel from the start, as the
orchestrator prompt permits, and unaffected by the M10 block. The openEMS +
gerber2ems toolchain builds, reproduces Antmicro's VNA-validated `stub_short` case
to **−1.1 % at 500 MHz / −1.7 % at the 5 GHz resonance**, and completes a
differential simulation of aipcb-routed geometry. M12 proper was never dispatched.
[ADR 0011](../decisions/0011-si-simulation.md).

## Headline numbers

| | |
|---|---|
| Milestones completed | **0 of 3** (M10 stopped, M11 not started, M12 phase 0 only) |
| Test suite | green throughout — 874 tests, `ruff` clean, `mypy --strict` clean on 60 files, unchanged from baseline |
| Code changed | **none** — every commit in this run is Markdown |
| One-pair SI simulation | **30 s – 2 min** (105 k–653 k cells, 16 cores) |
| Toolchain pin | gerber2ems `9eaf3033`, openEMS `a3058772`, image `sha256:e1921ec2…` |

Pipeline baseline measured across all ten examples (pre-pours, so it remains valid
as the regression baseline): mean `validate` 2.74 s, `build` 2.85 s, route 2.59 s,
`check` 0.94 s, `export` 3.38 s. Two facts worth carrying forward: `kicad-cli` costs
a flat **~1.05 s per board** regardless of complexity while routing reaches
**9.78 s** on `mcu-4layer`, and a **~1.5 s fixed floor** of symbol/footprint library
resolution sits under every single command, so far unexamined.

## Flagged deviations, all of them

1. **M10 implemented nothing.** M10a–M10d unbuilt; no schema, example, test or
   source file touched.
2. **M10a was deliberately not built although it alone was not blocked.** A
   `pours:` block emitting unfilled zones would make `aipcb export` ship Gerbers
   with the ground plane silently missing while `check` reported 0 DRC violations
   over it. Available as Option 3 in ADR 0009 if the owner disagrees.
3. **`docs/roadmap.md` was not updated** with M10's out-of-scope items, since doing
   so would assert M10 happened.
4. **M11 skipped entirely** — see above.
5. **M12 Phase 0 could not exercise `examples/pcie-sata`** (an M11 deliverable that
   does not exist); all its figures come from `examples/diff-pair`.
6. **Antmicro's `kicad-si-simulation-wrapper` was not evaluated**, so M12a's
   reuse-vs-reimplement decision stays open.
7. **M12's guardrail asks for a lockfile the repo carries**; today the pin lives
   only in ADR 0011.
8. **Phase 0 installed `podman` system-wide via apt** (rootless, no daemon). The
   project environment was not touched.
9. **`examples/diff-pair`'s declared 100 Ω target is unreachable** — Hammerstad
   puts its geometry at ~270 Ω differential. M12's acceptance criterion for it is
   not satisfiable as written; the example's target is wrong.
10. **The shared-pad-UUID defect is larger than `docs/roadmap.md` records** —
    `usb-port` emits 31 pads carrying 20 distinct UUIDs. Verified, not fixed, as
    instructed; it blocked neither M10 requirement.

## Recommended next action

**Pick an option in [ADR 0009](../decisions/0009-pours.md) § "The options".** Every
route to a filled zone goes through `pcbnew`, which [ADR 0001](../decisions/0001-kicad-io.md)
excluded *by the project brief* — "forces a KiCad runtime into CI and cannot run
headless cleanly". Half of that reason is now empirically stale: on 9.0.8 `pcbnew`
fills with no display, and `examples/usb-port` poured on `F.Cu`/`B.Cu` fills in
**0.42 s** with **0 DRC violations**. The half that stands is the CI requirement,
which is a cost imposed on everyone who builds this project, so it is the owner's
call and not a subagent's. ADR 0009 recommends **Option 1** — drive `pcbnew` in a
subprocess, recorded as a deliberate narrowing of ADR 0001 — and the orchestrator
independently confirmed that shape is the accurate one: `pcbnew` imports from the
*system* `python3` with `ZONE_FILLER` present, and does **not** import from the
project venv.

Once that is decided, M10 resumes from a clean boundary (`d6fcc49` is the last
pre-M10 commit) with its pre-code research already done, and M11 follows unchanged.

Two things to fix independently of that decision, both cheap and both already
diagnosed: `aipcb export` should write an `(aux_axis_origin)` — without it drill
files carry negative Y and **gerber2ems silently drops every via** — and the
position file should be named to match the `*pos.csv` convention, which likewise
fails silently today.

# Orchestrator log — M10 → M11 → M12

Run started 2026-08-21, unattended, from
[`docs/milestones/orchestrator-m10-m12.md`](../milestones/orchestrator-m10-m12.md).
Each milestone runs as a fresh subagent with no conversational memory; the repo is
the only thing carried between them. Gates below are verified by the orchestrator
itself, not taken from the subagents' word.

ADR numbers were assigned up front to keep concurrent agents from colliding:
`0009-pours.md` (M10), `0010-highspeed.md` (M11), `0011-si-simulation.md` (M12).

## Baseline before M10

Starting point: `f4e3868`, working tree clean, `master` up to date with `origin`.

Verified by the orchestrator before anything was delegated:

| Check | Result |
|---|---|
| `pytest -q` | green, exit 0 (874 tests, 1 skipped) |
| `ruff check .` | All checks passed |
| `mypy --strict` | no issues, 60 source files |
| `kicad-cli version` | 9.0.8 |

The M12 Phase 0 subagent (environment verification only — no repo code) was
dispatched in parallel at this point, as the orchestrator prompt permits.

## M10 — copper pours, stitching vias & plane integrity

Dispatched to a fresh subagent with the full text of
`docs/milestones/m10-pours.md`.

**Verdict: STOPPED on the milestone's own named stop-condition. Chain halted; M11
was never dispatched.**

M10's guardrail reads: *"If KiCad's CLI cannot fill zones headlessly in the
installed version, stop and present options before working around it."* The
subagent reported that it cannot. The orchestrator verified this independently
rather than taking the subagent's word:

- `kicad-cli pcb --help` offers exactly `{drc, export, render}` — no fill, no
  refill subcommand.
- `kicad-cli pcb drc --help` has no fill-related flag; `pcb export gerbers --help`
  matches nothing for `fill|zone|refill`; `jobset` offers only `run`.
- The subagent's positive probe (a `GND` zone spanning two vias 24 mm apart:
  unconnected stays 1 through DRC unfilled, drops to 0 only after a real fill;
  `F.Cu` gerber 596 bytes/2 records unfilled vs 1066/20 filled) is consistent with
  that surface. An unfilled pour exports as no copper, silently.
- The escape hatch the ADR recommends is real: `pcbnew` imports from the *system*
  `python3` (9.0.8+dfsg-1) with `ZONE_FILLER` present, and does **not** import from
  the project venv — so Option 1's subprocess shape is the accurate one.

This is a genuine stop-the-chain condition and not a judgment call the orchestrator
may absorb: every live option either reverses or qualifies
[ADR 0001](../decisions/0001-kicad-io.md), which excluded `pcbnew` **by the project
brief**, not by implementation convenience. Choosing to put a KiCad runtime into CI
for everyone is the owner's call.

Gate status at the halt:

| Gate | Result |
|---|---|
| 1 — suite, ruff, mypy | green; the two commits touch only Markdown (`git diff --stat f4e3868..HEAD` = 3 doc files), so the verified baseline still holds |
| 2 — acceptance checks | **not applicable — nothing was implemented**, so there is nothing to accept |
| 3 — delivery report | `docs/reports/m10.md` exists (230 lines) and does carry measurements: fill determinism over 8 runs on 2 boards, the ten-example per-stage performance table, the usb-port fill probe (0.42 s, 444 vertices, 0 DRC violations) |
| 4 — commit + push | `6c73140`, `6a25da9` — pushed |
| 5 — this entry | written |

Notable, and carried into the final report:

- **Nothing was implemented.** M10a–M10d are all unbuilt; no schema, example, test
  or source file was touched.
- The subagent **declined to build M10a alone** even though it was not blocked,
  reasoning that a `pours:` block emitting unfilled zones would make `aipcb export`
  ship Gerbers with the ground plane silently missing while `check` still reported
  0 DRC violations over it — a regression dressed as a feature. The orchestrator
  agrees this was the right call, and notes it is available as ADR Option 3 if the
  owner disagrees.
- `docs/roadmap.md` was deliberately **not** updated with M10's out-of-scope items,
  since doing so would assert M10 happened.
- **Shared-pad-UUID issue (the orchestrator asked for it to be verified, not
  fixed):** the defect is real and larger than `docs/roadmap.md` describes —
  `usb-port` emits 31 pads carrying only 20 distinct UUIDs, with twelve pads
  numbered `6` sharing one UUID. It blocks neither M10 requirement, for two
  independent reasons: `route.obstacles` keys on `reference#index` (`J1.6#7`,
  `J1.6#9`), never on the UUID; and `zone_connect` is a positional token inside the
  pad's own s-expression, so a `thermal_pad` role needs no UUID lookup. Untouched,
  as instructed.

## M11 — high-speed

**Not dispatched.** M11e's reference-plane checks consume M10's filled zones; a
reference-plane check over a board with no plane copper checks nothing. Starting it
would produce work that cannot be verified.

## M12 — SI simulation

**Phase 0 only: PASS.** Dispatched in parallel at the start, as the orchestrator
prompt permits, and finished after the chain had already halted. M12 proper was
never dispatched — it depends on M11's routed pairs, which depend on M10.

Phase 0 was scoped to environment verification and honoured that scope: the only
repo file it created is `docs/decisions/0011-si-simulation.md` (351 lines), it ran
no git commands, and it installed nothing into `.venv` or `pyproject.toml`. The
orchestrator verified the artifacts it claimed: `podman` present at
`/usr/bin/podman`, image `localhost/gerber2ems:phase0` = `e1921ec2b3fc`, matching
the digest recorded in the ADR. Committed as `451812c` and pushed.

The verdict is genuinely useful even with the chain stopped, because it retires the
milestone's biggest unknown:

- The toolchain builds and reproduces Antmicro's own VNA-validated `stub_short`
  case to within **−1.1 % at 500 MHz** and **−1.7 % at the 5 GHz stub resonance**
  (332.0 vs 337.7 Ω). The solver is sound.
- **One pair simulates in 30 s – 2 min** (105 k–653 k cells, 16 cores), against the
  milestone's own "minutes to hours" assumption. M12 is a CI stage, not an
  overnight batch — that reshapes the design M12b was told to build around.
- Antmicro publishes **no prebuilt image**; the "container route" is a from-source
  build inside a Dockerfile (~7 min).

Two findings that should change M12's plan when it eventually runs, both flagged
rather than worked around:

- **Impedance numbers are not yet trustworthy.** Three port/mesh configurations of
  the *same* geometry produced ≈26 Ω, ≈340 Ω and ≈950 Ω, and the coarse run gave a
  non-physical |Sdd21| > 1. The cause is the structure, not the solver: every
  gerber2ems example sits 0.12 mm above its plane, while `diff-pair`'s nearest
  copper is 1.51 mm away. M12a should calibrate on `examples/mcu-4layer` (GND
  0.48 mm below F.Cu).
- **`examples/diff-pair` cannot reach its declared 100 Ω.** Hammerstad puts it at
  Z₀ ≈ 135 Ω single-ended, ~270 Ω differential. M12's acceptance criterion
  "diff-pair results within plausible range of its design target" **is not
  satisfiable as written** — the example's target is wrong, and reporting that is
  the correct outcome rather than a failure to engineer around.

Export-format gaps found (the make-or-break compatibility test), of which **two
fail silently** — M12 must assert that gerber2ems saw what it was handed:

| # | Gap | Severity |
|---|---|---|
| 1 | `stackup.json` not emitted — but fully derivable, aipcb already writes `epsilon_r`/`loss_tangent` into the `.kicad_pcb` | ~30 lines, no information loss |
| 2 | Drill filename: one `MixedPlating` `.drl` vs gerber2ems's `-PTH.drl` glob → `sys.exit(1)` | loud; fix is `--excellon-separate-th` |
| 3 | **No `(aux_axis_origin)`, so drill lands in absolute page coords with negative Y; gerber2ems's regex cannot match negatives** | **silent — every via dropped** |
| 4 | `positions.csv` does not match the `*pos.csv` glob | **silent — never seen** |
| 5 | Simulation ports (`SP<n>`/`Simulation_Port`) have no concept in aipcb | design work for M12a |
| 6 | `netinfo.json` missing | optional, nearly free |
| 7 | `aipcb export` does not route — `F_Cu.gbr` had 16 pad flashes and zero track draws | needs `route all -o DIR` then `export --build-dir DIR` |

Not done in Phase 0, and correctly so: `examples/pcie-sata` was never exercised (it
is an M11 deliverable that does not exist), and Antmicro's
`kicad-si-simulation-wrapper` was not evaluated, so M12a's reuse-vs-reimplement
call stays open. The guardrail's "lockfile the repo carries" is satisfied only by
the ADR today; a real lockfile is M12-proper work.

Note a system-level side effect: Phase 0 installed `podman` system-wide via apt
(rootless, no daemon). Nothing in the project environment was touched.

## Chain end

Halted after M10. Final report: [`chain-m10-m12.md`](chain-m10-m12.md).

---

# Resumption — ADR 0009 decided

The project owner decided the blocking question on 2026-08-21 and the chain
resumed. Recorded here so the halt and the restart read as one story.

**Decision: ADR 0009 Option 1** — fill via a `pcbnew` subprocess — approved with
four binding conditions (rationale, version lock, bridge-not-turn framing,
subprocess hygiene). Written into [ADR 0009](../decisions/0009-pours.md) under
"The decision"; its status moved from *Proposed — blocked* to *Accepted*. The
orchestrator also recorded the owner's cultural note in a new `CLAUDE.md`: ADR
premises about external tools' capabilities have expiry dates, twice observed now
(kiutils, headless fill).

**M12 spec updated before dispatch** (`docs/milestones/m12-simulation.md`): Phase 0
marked complete so the milestone agent does not redo it, the "minutes to hours per
pair" framing replaced with the measured 30 s – 2 min, the M12b parallelism call
handed to the agent as a measured judgement rather than an assumed requirement,
and Phase 0's two plan-changing findings promoted into the spec body — calibrate on
`mcu-4layer` rather than `diff-pair`, and `diff-pair`'s declared 100 Ω target is
unreachable for its geometry, so that acceptance criterion is not satisfiable as
written.

**Prerequisites dispatched before M10**, as separate commits: the missing
`aux_axis_origin` and the position-file naming. Both are silent-corruption
defects — wrong output, no error — which is the class this project exists to
eliminate, so they are fixed with tests before the milestone that will exercise
them.

### Prerequisite gate — PASSED

Both fixes verified by the orchestrator independently, not taken on the subagent's
word:

- **Golden churn contains nothing but the intended change.** Reducing the whole
  `tests/golden/` diff to its distinct lines yields exactly one form, ten times:
  `+ (aux_axis_origin N N)`. No coordinate rebasing inside the board files — correct,
  since board space is unchanged and only exported fab data is rebased — and no
  `.kicad_sch` / `.kicad_pro` churn.
- **Drill coordinates re-measured from a fresh route+export of `mcu-4layer`:**
  0 negative coordinates, and **all 23 coordinate lines match gerber2ems's own
  `X([0-9]+\.[0-9]+)Y([0-9]+\.[0-9]+)` regex** — the specific thing that was
  silently dropping every via.
- **Placement file:** the export now produces `mcu-4layer-all-pos.csv`, which the
  consumer's `*pos.csv` glob finds. The subagent checked four authorities before
  changing anything and found aipcb was the wrong one: gerber2ems's source
  (`importer.py:295`, `config.py:339`), `kicad-cli` (which has no convention of its
  own — not the authority), KiCad's own `place_file_exporter.cpp` (`<board>-all-pos.csv`
  for a both-sides CSV), and Antmicro's example boards.
- **Suite:** `pytest` exit 0, 886 tests (874 + 12 new), `ruff` clean,
  `mypy --strict` clean on 60 files.

**A third defect was found and deliberately not fixed** — the right call, recorded
so it cannot be lost. The placement file's *coordinates* are still absolute page
coordinates with negative Y (confirmed by the orchestrator: `"C1",…,145.500000,-122.500000`
on `mcu-4layer`), because `export.py` does not pass `--use-drill-file-origin` to
`pcb export pos`. It is harmless today only because aipcb emits no `SP<n>` rows for
anything to read — but the file is now *discoverable*, so it will be read in the
wrong frame the moment ports exist. It is ADR 0011 requirement #6 and squarely
M12a's port work; it has been written into `docs/milestones/m12-simulation.md` as
required work rather than fixed here, because choosing the coordinate frame of a
fab deliverable is a real decision, not a one-flag mechanical fix.

Commits `df118ba`, `7ea4fc5`, pushed.

### M10 gate — PASSED (all five)

Re-run by the orchestrator, not taken from the subagent's report.

| Gate | Result |
|---|---|
| 1 — suite, ruff, mypy | `pytest` exit 0 (968 collected, up from 886); `ruff` clean; `mypy --strict` clean on **66** source files (60 before) |
| 2 — acceptance checks | passed, measured independently — see below |
| 3 — delivery report | `docs/reports/m10.md`: deviations in a table at the top, measured numbers throughout, the required per-stage performance table with aipcb stages separated from `kicad-cli`/`pcbnew` calls and compared line-by-line against the pre-pours baseline |
| 4 — commit + push | `1973b4a` (implementation), `7c19421` (docs + report) — pushed |
| 5 — this entry | written |

**Gate 2, measured by the orchestrator on all ten examples:**

- **Build byte-stable, unfilled:** built each example twice into separate
  directories and diffed — 10/10 identical, and **`filled_polygon` count is 0 in
  every build output**. The stability policy holds as a property of the file, not
  as a promise.
- **Zones present where they should be:** 8 of 10 carry zones (`mcu-4layer` has 3,
  the split plane); `congestion` and `overconstrained` have **0**, which is both the
  documented deviation and the live backward-compatibility witness — a design
  without `pours:` still emits no zone at all.
- **Check, filled: zero errors on all ten**, `rc=0` throughout. Severity counts from
  `--json`: only `info`, plus 1 `warning` on `qfn-fanout` (the `min_contiguous`
  fragmentation warning, as designed), 1 on `usb-port`, and 3 on `overconstrained`
  (its expected hand-overs). No errors anywhere.
- **Export ships filled copper:** `usb-port` F.Cu **1758** coordinate records —
  independently reproducing the report's figure — `led-blinker` 1410,
  `mcu-4layer` 2946, each with `G36` region-fill commands present. An unfilled pour
  would have plotted as no copper and looked like a correct plot.

**The four binding ADR 0009 conditions were checked in the code, not just in the
prose:** the version lock compares `pcbnew.GetBuildVersion()` against
`kicad-cli version` on the numeric prefix (Debian reports `9.0.8+dfsg-1`) and is
enforced twice — in `fill_board` before spawning and inside the subprocess via
`--require-version`, which exits refusing to fill; interpreter resolution is probed
in a documented order rather than assumed; and
`test_a_check_reports_it_rather_than_checking_an_unfilled_board` closes the
silent-corruption path by asserting that a `python3` that cannot import `pcbnew`
makes `check` report the failure **and not run DRC at all**, with no output file
written.

Notable, carried to the final report:

- **The subagent corrected its own earlier Finding 3 rather than defending it.** The
  stop report measured the fill byte-deterministic; closer measurement shows the
  fill *geometry* is byte-identical while the filled *file* differs every run by
  exactly 12 lines — UUIDs of `Datasheet`/`Description` properties KiCad's writer
  adds itself. This is precisely why the guarantee is scoped to unfilled build
  output. No UUID aipcb emits is lost across the fill (279 in, 279 out).
- **KiCad's own default thermal settings (0.5/0.5) produce boards KiCad's own DRC
  rejects** on 4 of 8 poured examples. aipcb defaults to 0.25/0.5 instead, measured.
- **New CI prerequisite:** the `kicad` package, not only the `kicad-cli` binary.
  Confined — a design without `pours:` never invokes it — but real, and flagged at
  the top of the report rather than in a footnote.
- **Pad-UUID aliasing re-verified against the new code:** `usb-port` still emits 12
  pads numbered 6 sharing one UUID, unchanged and un-worked-around; exactly one of
  the twelve carries `(zone_connect 2)`, asserted by a test that *also* asserts the
  twelve still share a UUID. Stitching never reads a pad UUID.
- Deviations, all at the top of the report: 8/10 examples poured; `thermal_pad` as
  the explicit list the spec permits rather than a role; the fragmenting case split
  between a synthetic board (exact counts) and `qfn-fanout` (the real warning);
  keepout zones built but exercised only by a purpose-built test; suite runtime
  10 m 58 s → 13 m 57 s; and `--no-route` now saying louder that it checks a
  different board.

## M11 — high-speed

Dispatched to a fresh subagent with the full spec plus two binding kickoff
warnings: the pad-UUID aliasing (which M11b's own spec text predicts will bite at
the edge connector), and an instruction to *verify* whether the M11d rules resolve
the known `diff-pair` copper sliver — flagging it loudly if they do not, rather
than retiring the marker or weakening the test.

**Infrastructure failure, not a stop-condition.** The implementation session was
terminated by an API session limit at the moment it printed "Now the performance
measurements:". All of M11a–M11e, the tests, the docs and
[ADR 0010](../decisions/0010-highspeed.md) were already committed across five
commits (`4599b95`, `fbd048e`, `166fa4f`, `e33b0ce`, `1e0a6a2`) with a clean tree;
only `docs/reports/m11.md` was missing. That is a gate-3 failure, so the
orchestrator spent its **one permitted remediation pass** on exactly that: a fresh
subagent to write the report and take the measurements the first session died on.
The remediation was scoped to documenting and measuring the delivered code, not
re-implementing it. The orchestrator deliberately did **not** run the test suite
while that pass was working, so its wall-clock numbers are taken on a quiet machine.

Two things the orchestrator established from the repository before remediating,
so they could not be quietly smoothed over in a reconstruction:

- **The `diff-pair` copper sliver was not resolved.**
  `tests/test_check_loop.py` still carries
  `"diff-pair": frozenset({"kicad-copper-sliver"})`. The kickoff expected M11d
  rule 1 (the standoff corridor) to fix it. It did not. To the M11 session's
  credit the marker was left standing rather than silenced — which is the honest
  outcome — but it is an expectation that was not met and it is recorded as such.
- **`pcie-sata` carries four accepted known issues**, each with reasoning written
  into the test file rather than hidden: `diff-pair-wall-hugging`,
  `diff-pair-skew`, `hs-skew` and `kicad-lib-footprint-mismatch`. The first is
  precisely the outcome M11d rule 2 specifies (flag stands after one failed
  re-tighten). The skew items need scrutiny at the gate: a pair delivered 0.25–0.29 mm
  out against the 0.125 mm its own class declares sits close to the "marginal
  delivery" this milestone exists to prevent — the mitigating reading being that
  skew is an M11e *reporting* item rather than one of the three M11d rules whose
  breach forces a hand-over.

Also established before the gate: **`src/aipcb/route/stretch.py` is completely
untouched** by M11. The M11d rules landed in `route/pairs.py` (+418 lines), the
coupled-pair path. That is a more conservative reading of "M11d is the only
permitted stretcher change" than the guardrail required.

### M11 gate — PASSED, with one acceptance clause explicitly NOT met

Re-run by the orchestrator. The remediation pass produced `docs/reports/m11.md`
(722 lines, commit `df90927`).

| Gate | Result |
|---|---|
| 1 — suite, ruff, mypy | `pytest` exit 0 (**1090 tests**, up from 968; 20 m 27 s); `ruff` clean; `mypy --strict` clean on **75** source files |
| 2 — acceptance checks | passed as specified — but see the shortfall below |
| 3 — delivery report | passes emphatically: 11 deviations in a table at the top, measured claims throughout, the performance table compared against M10's baseline |
| 4 — commit + push | five M11 commits plus `df90927` — pushed |
| 5 — this entry | written |

**Gate 2, measured by the orchestrator across all eleven examples:**

- **Byte-stable builds, 11/11** (built twice into separate directories and diffed),
  `filled_polygon` = 0 in every build output.
- **Zero DRC/ERC errors on all eleven**, `rc=0` throughout. Warnings only:
  `pcie-sata` 10, `overconstrained` 3 (its expected hand-overs), `qfn-fanout` 1,
  `usb-port` 1.
- **Prior examples genuinely unaffected:** `git diff b7d9c5e..HEAD -- tests/golden/`
  adds the new `pcie-sata` golden and modifies **no** pre-M11 golden at all.
- **The stretcher guardrail held strictly.** `route/stretch.py` is untouched. The
  three M11d rules live in `route/pairs.py`, gated on `impedance_diff_ohm`, with
  the docstring stating that everything else "takes exactly the path it took
  before". Three rules, no fourth — checked in the code, not in the prose.

**The shortfall, recorded rather than waved through.** The specification's
acceptance says *"the M11e report runs clean … on everything that routed"*. It does
not. Reference continuity and via stubs are clean — 629.3 mm projected, **0** plane
crossings, worst stub **0.000 mm** against a 0.5 mm threshold, width/gap deviation
0.0 on all twelve pair objects — but `REFCLKP/N` comes out **0.292 mm** and
`PCIE_RXP/N` **0.256 mm** against the **0.125 mm** their class declares, and both
are *delivered* rather than handed over via M9f.

The orchestrator passes the gate on this, and the reasoning should be legible to
whoever reads it later:

- Gate 2 as this orchestrator defines it — 0 DRC/ERC violations and byte-stable
  rebuilds — is met without qualification.
- **Nothing is silent**, which is the failure mode this milestone actually exists to
  prevent. Six warnings carry the number, the JSON carries it, and
  `tests/test_check_loop.py` records it as a named known issue. The budget was
  demonstrably not widened to fit: the source records that the SATA class's first
  draft was 0.127 mm, that one pair missed it by 49 µm, and that the class was
  rewritten with an argument rather than tuned until the warning disappeared.
- Skew is an M11e *reporting* item, not one of the three M11d rules whose breach
  forces a hand-over — and `verify: error` exists to promote it, unused here.
- The report itself argues both sides and concedes the point rather than defending
  it, including a first-principles estimate that 0.292 mm is ≈1.8 ps against a
  125 ps Gen3 unit interval.

It is an unmet acceptance clause, flagged at the top of the delivery report and
here, and **it is the project owner's call whether that is acceptable** — the
orchestrator's job was to make sure it could not pass unnoticed.

**The diff-pair sliver expectation was not met, and the explanation outranks the
expectation.** Measured: the routed, *unfilled* board still carries exactly one
`copper_sliver` warning (0 once filled). M11d rule 1 could not have closed it — the
rule applies only to classes naming an `impedance_diff_ohm`, and `diff-pair`'s
`lvds` class names none (`standoff` 1.0, `target_ohm` null), so M11d never ran on
that board at all. The marker was left standing rather than retired or silenced.
Not verifiable: *which* copper the sliver lies between — KiCad 9.0.8 reports this
violation with an empty `items` array and no position in both output formats.

**Pad-UUID aliasing:** the card edge is not affected — `J1` has 36 pads, 36 numbers
and **36 distinct UUIDs**. The defect remains live elsewhere on the same board (JST
shells 9 pads/8 UUIDs, QFN exposed pad 65/50) and `usb-port` is unchanged at 31/20.
Commit `1e0a6a2` turned out to be about something else: the *generated* card-edge
pour keepout, the one zone nobody declares, which the UUID index did not know
about — so a DRC violation on it reported `loc: None` and "probably added by hand
in KiCad". Now it points at `design.yaml:451:3`.

**Two open defects carried forward, neither fixed:**

- **`_repair` can route across another net** (`route/plan.py`) — found by the M11
  session on PRSNT, unexplained, recorded in `docs/roadmap.md`. It does not
  manifest on any bundled example today. This is a correctness bug in the router
  and the most serious open item in the milestone.
- **The same pair is reported out of budget three times with two different
  numbers** — 0.251 from `_match_lengths`, 0.292 from `measure_skew`, and again as
  `hs-skew` — with nothing explaining why they differ. Found by the remediation
  pass while measuring; reported, not fixed.

**Other deviations of consequence** (all eleven are at the top of the report):
`standoff_k`'s default of 3 refuses every pair on the milestone's own reference
board, which sets 1.4; four spec field names were renamed and `class: diff_pair`
was not implemented; **PERST# and the A1–B17 presence-detect strap are absent from
the netlist**, so "90/90 routed" omits two signals a real card needs; **no pair was
handed over at all**, so the reference board never exercises the M9f path; AC
capacitors are validated but never placed; impedance derivation is microstrip-only
and an inner-layer class silently gets a microstrip width back; suite runtime
13 m 57 s → 20 m 27 s.

**No regression against the M10 baseline:** validate +1.2 %, build +0.9 %, route
+0.8 %, export +1.3 %, check −1.5 %; largest per-example delta 0.14 s. `pcie-sata`
`check` is 44.4 s, 89 % of it routing; M11d re-tightening adds **0.139 s (0.35 %)**
and M11e analysis **0.111 s (0.28 %)**. The report notes rule 1 is not separable —
it widens the A* corridor. It also corrects M10's "fixed ~2.5 s floor": measured
0.88–4.17 s, tracking footprint-library count.

## M12 — signal-integrity simulation

Dispatched with the spec as updated against Phase 0's measurements, the assignment
of the remaining export gaps, and the instruction that simulation is the natural
discriminator for the two pairs M11 left open.

**Second infrastructure failure of the run.** The implementation session was killed
by an API 529 after committing all fourteen of its commits, but *before* finishing
its report: `docs/reports/m12.md` twice pointed at a pcie-sata "batch table above"
that did not exist, and its section on the two skew pairs established that Scd21 is
the right metric and then stopped without numbers. That is a gate-3 failure, so the
orchestrator spent its **one permitted M12 remediation pass** on the report gap.
The surviving Touchstone data in the scratch directory made this recoverable
without re-deriving anything by hand.

| Gate | Result |
|---|---|
| 1 — suite, ruff, mypy | `pytest` exit 0 (**31 m 58 s**); `ruff` clean; `mypy --strict` clean on **85** source files |
| 2 — acceptance checks | passed, measured by the orchestrator: **11/11 examples byte-stable**, **0 DRC errors** on all eleven, warning counts unchanged from before M12 (pcie-sata 10, overconstrained 3, qfn-fanout 1, usb-port 1) — the new work disturbed no prior example. One acceptance item was not run; see below |
| 3 — delivery report | passes after remediation |
| 4 — commit + push | fourteen M12 commits, plus `0ba14ae` (remediation) and `db1f5ba` (orchestrator deviation row) — pushed |
| 5 — this entry | written |

The remediation ran **all eleven links** — roughly two hours of solver time — so
nothing is marked not-simulated, and it verified the numbers come from the tool's
own cache path rather than a parallel calculation.

**The answer to the question M11 left open: simulation does not discriminate those
two pairs — and on the reported verdict it does worse than not discriminating.**

| Skew | Pair | Δτ | Worst \|Scd21\| | Verdict |
|---|---|---|---|---|
| 0.000 mm | `PCIE_TX` | 0.00 ps | −26.6 dB | pass |
| **0.004 mm** | **`SATA3_RX`** | 0.03 ps | **−14.3 dB** | **warn** |
| **0.042 mm** | **`SATA3_TX`** | 0.28 ps | **−15.7 dB** | **warn** |
| **0.256 mm (over budget)** | **`PCIE_RX`** | 1.82 ps | **−25.2 dB** | **pass** |
| **0.292 mm (over budget)** | **`REFCLK`** | 2.06 ps | **−23.0 dB** | **pass** |

Every `warn` is a pair comfortably inside its budget; both budget-busting pairs
pass. The cause is a mode-conversion floor spanning **more than 25 dB** across
nominally identical links, in a quantity the skew under test moves by about 3 dB.
Read across frequency rather than as a scalar, `REFCLK` *does* show a clean skew
signature — climbing 17 dB from 1 to 7 GHz and tracking `|sin(πfΔτ)|` for its
measured 2.06 ps to within 3.2 dB — so the physics is visible even though the
published verdict is not. M11's own physical argument is corroborated; the
acceptance question still gets a "no".

**What this milestone found out about the milestone before it.** Simulated
impedance sits **systematically below every declared target** — `REFCLK` at 50.9 Ω
against 85 Ω (−40 %), `SATA3_TX` at 62.5 Ω against 100 Ω (−37.5 %), all eleven
links low. The cause is not the router: every bundled example pours ground up to
its pairs, while **both closed-form models aipcb derives widths from are bare
microstrip** and know nothing about coplanar ground. The calibration pair on
`examples/mcu-4layer` reads ≈74 Ω where those formulas predict 121–127 Ω. So M11's
derived widths are systematically narrow for the boards this tool actually builds.
Recorded in `docs/roadmap.md`, deliberately not fixed — fixing it means a CPWG
model and regenerating every controlled-impedance example.

**Deviations of consequence** (the report's table has 19 rows):

- Eight `sata` links, identical in class, width, gap, layer and length to within
  0.5 mm, **simulate 29 Ω apart** (62.5–91.9 Ω). The remediation pass could not
  separate a real spread from an unconverged extraction, and says so. This is the
  tightest honest bound in the report on what any single number is worth.
- **11 of 22 insertion-loss figures are positive** — gain, i.e. truncation noise.
  `insertion_loss` reads `pass` on all eleven because its threshold is ≥ −3 dB, so
  that verdict means "no evidence of excess loss", not a measurement.
- `mode_conversion_db` is a worst-in-band scalar over **each class's own band**, so
  an 8 GHz `pcie` class and a 6 GHz `sata` class are not compared over the same
  interval; 4 of 11 peak at the noisy bottom of the band.
- The `.kicad_pcb` stackup disagrees with the source stackup (three equal 0.48 mm
  dielectrics vs. the declared 0.2104/1.065/0.2104). Found here, deliberately not
  fixed — it would change every existing board, which M12's guardrails forbid.
  Simulation uses the source stackup and says so.
- `aipcb export` now writes **two** drill files where it wrote one, deliberately:
  gerber2ems globs `*-PTH.drl` and exits 1 on an empty glob.
- **The "deliberately degraded case" acceptance item was not run**, and the report
  did not list it. The orchestrator added the row (`db1f5ba`) rather than leave an
  acceptance item that is neither delivered nor visible. What exists instead is an
  *accidental* degraded case with a measured before/after (the collinear-launch
  defect: peak \|Sdd21\| 7.26 → 1.14, impedance 436.6 Ω → 66.6 Ω) plus unit-level
  checks. That shows the machinery discriminates a broken *slice*; it does not show
  it discriminates a deliberately worsened *layout*, which is what was asked — and
  it matters more than it would otherwise, given that the one real discrimination
  question in this milestone was answered "no".

**One open defect, reported not fixed:** `si/runner.py` cleans up its named
container only on `subprocess.TimeoutExpired`. When the client is killed outright —
which happened twice in this run — the container survives. The remediation pass
found the dead session's container still running sixteen cores of FDTD, and its
first relaunch had *two* containers writing the same `ems/` directory. A pre-flight
check for a live `aipcb-si-*` container on the same work directory would cost
nothing.


---

# Orchestrator log — M17 → M18

Run started 2026-08-24, unattended, from the chain prompt for
[`m17-measured-improvements.md`](../milestones/m17-measured-improvements.md) and
[`m18-literature-survey.md`](../milestones/m18-literature-survey.md). Same pattern as
the M10–M12 chain: a fresh subagent per milestone with no conversational memory, the
repository as the only thing carried between them, gates verified by the orchestrator
itself rather than taken from the subagent's word.

**The chain ends after M18 by design.** M18's second deliverable is a *draft*
milestone for the owner to review; implementing unreviewed research candidates is
exactly the improvisation this project does not do.

## Baseline before M17

Starting point: `6b6a963`, `master` up to date with `origin`, the two milestone
prompts untracked. They were committed as `f2cf31d` before dispatch so the chain
starts from a clean tree and each spec sits in the history it is judged against.

Verified by the orchestrator before anything was delegated:

| Check | Result |
|---|---|
| `pytest -q` | green, exit 0 (1 327 tests collected) |
| `ruff check .` | All checks passed |
| `mypy --strict src` | no issues, 95 source files |
| `bench/results/baseline.json` | M16 baseline, commit `aa0db94`, clean |

## M17 — the measured candidates

Dispatched to a fresh subagent with the full text of
`docs/milestones/m17-measured-improvements.md`.

**Verdict: PASSED all five gates. Three candidates landed, nothing rejected.**

### Gate 1 — suite, ruff, mypy: PASS

Run by the orchestrator on the committed tree, not read from the report:
`pytest -q` exit 0 (1 332 tests, 2 `AIPCB_FULL_CORPUS` skips), `ruff check .` clean,
`mypy --strict src` clean over 96 source files.

### Gate 2 — every landed candidate inside its budget: PASS

`aipcb bench --compare` against the M16 baseline, extracted from `6b6a963` and run by
the orchestrator on its own:

```
improvement: pcie-sata: routing 35.87 -> 16.20 s (-55%)
improvement: mcu-4layer: routing 10.14 -> 5.05 s (-50%)
improvement: pcie-sata: copper 1108.1 -> 1077.8 mm (-2.7%), vias 39 -> 35,
             layer changes 42 -> 38, self-crossings 1 -> 0
change:      pcie-sata: the board changed (0e8f21b16227 -> 8046bb72f60f)
exit 0 — no regressions
```

Per-board `tighten` stage, M16 → M17, computed by the orchestrator from the two
committed results files:

| Board | tighten M16 | tighten M17 | Δ | hash unchanged |
|---|---:|---:|---:|:--:|
| pcie-sata | 33.883 s | 15.035 s | **−55.6%** | no (§1.2 explains) |
| mcu-4layer | 8.553 s | 4.063 s | **−52.5%** | yes |
| qfn-fanout | 5.013 s | 2.454 s | −51.0% | yes |
| enclosure | 4.079 s | 1.862 s | −54.4% | yes |
| corpus | 54.912 s | 25.659 s | **−53.3%** | 10 of 11 |

- **M17c** (≥ 25% stretcher-time reduction, no quality regression): held with room
  to spare on both slowest boards, and ten of eleven board hashes are byte-identical
  to M16 — the speed came with no change in what the router decided.
- **M17a** (≤ +10% runtime): the whole new `reclaim` stage costs **1.039 s** across
  the corpus against a 61.863 s M16 baseline = **+1.7%**, measured on the
  orchestrator's own run.
- **M17b** (≤ +5% on the board under test): `reclaim` on `pcie-sata` is 0.450 s
  against that board's 35.87 s M16 routing time = **+1.3%**.

Re-baseline: `bench/results/baseline.json` now names commit `3b59a52` with no
`-dirty` suffix, the M16 file stays identifiable at `6b6a963`, and report §5 carries
the metric-by-metric "why the new numbers are better" table that
`bench/results/README.md` demands. Gate met as written.

### Gate 3 — the E2 case on pcie-sata: PASS

Verified independently by routing the board through the CLI rather than by reading
the report: `aipcb route all examples/pcie-sata/design.yaml --json` emits fourteen
diagnostic codes and **`route-doubles-back` is not among them**. The named defect is
in the reclaim record by name —
`GND U1.17>U1.49 gave back a retrace, 2 vias and 17.116 mm of copper on F.Cu`.

The guard is retained rather than deleted:
`TestSelfCrossingInvariant::test_the_corpus_carries_no_doubling` still routes
`pcie-sata` for real and now asserts the count is zero, and the seven unit tests that
pin what the detector considers a finding are untouched. `tests/test_check_loop.py`
lost the `KNOWN_ISSUES` entry with a comment in its place saying why.

### Gate 4 — delivery report with measurements: PASS

`docs/reports/m17.md`, 452 lines, opens with the per-candidate budget scoreboard and
a seven-row deviations table, and carries the per-board via table, the profile
findings, the re-fitted scaling models and the new extrapolation. Measurements, not
assertions.

**One orchestrator correction.** The scoreboard cell for M17b read `+1.6%` where
§2.4 derives `+1.3%` from 0.450 s against 35.87 s. Both are inside the +5% budget so
nothing about the verdict moves, but a report whose two statements of one measurement
disagree is exactly the thing this project's culture is against. Corrected to the
derived figure with the derivation inline. Precedent: the orchestrator added a
missing acceptance row itself in M12 (`db1f5ba`) rather than leave it invisible.

### Gate 5 — pushed, and this entry

`3b59a52` and `12b1e47` were pushed to `origin/master` by the subagent; the
orchestrator's correction rides with this entry.

### Findings worth carrying, beyond the pass

- **The 37-of-90 estimate overstated the opportunity by an order of magnitude.** Of
  55 candidate spans corpus-wide, 2 collapse. 21 were rejected because the on-layer
  route is *longer* and 32 because the free space is genuinely split — and **zero on
  capacity**. M16's "layer changes made where no corridor was half full" was a proxy
  for opportunity and the geometry disagreed with it. This router's vias are, with
  four exceptions, load bearing.
- **The capacity arbiter never fired.** It is built, wired in and unit-tested, and
  the corpus gave it nothing to reject. The report states that rather than claiming a
  win, which is the right call; it also means the arbiter has no real-board evidence
  behind it and is the first thing to re-measure if a denser example is ever added.
- **The stretcher's time was not where its name said.** Two thirds of "tightening"
  was `build_field` → `_via_sites`, rebuilt per repaired connection — 1.9 M Shapely
  `Point` constructions on one board. The cut is batching, and **no algorithm
  changed**: the log-log exponents moved 1.72 → 1.70 and 1.12 → 1.10. The shape of
  the curve is exactly where M16 left it, which is the evidence that M17 did not
  answer M18's question.
- **The old capability-ladder number does not reproduce.** The M17 brief cites
  "≈ 31 min" for a 900-connection board and M16 says "roughly half an hour"; neither
  is reproducible from the committed baseline by M16's own stated method, which gives
  23.7 min. The report states the method and publishes **23.7 → 11.3 min**, both
  computed from committed results files. Taking the honest pair over the remembered
  number is the right resolution, and it is flagged here because a capability figure
  that quietly changes meaning is worse than a wrong one.
- **M17a and M17b turned out to be one mechanism**, because every retrace in the
  corpus *is* a via pair returning to its own layer. No separate trimmer was written
  and the report says why rather than shipping dead code for symmetry.

### Open items handed to the owner, not decided by the orchestrator

1. `ROUTER_STAGES` gained `reclaim` and `route all --json` gained
   `routing.reclaimed` — deliberate interface additions that want confirming as
   permanent.
2. `docs/images/*` are stale by four vias on `pcie-sata`. Left alone: regenerating
   them is large binary churn for an invisible difference, and the call on when to
   spend it is the owner's.
3. The capacity arbiter's lack of real-board evidence, above.

None of these blocks M18, which touches no code, so the chain continues.

## M18 — the literature survey

Dispatched to a fresh subagent with the full text of
`docs/milestones/m18-literature-survey.md`.

**Verdict: PASSED all four gates. Research only, as specified.**

### Gate 1 — the note holds the toporouter-note discipline: PASS

`docs/notes/routing-literature.md`, 1 269 lines, opens with a licence-wall
restatement and a **"Sources, and what could not be got"** table that gives every
source a reachability status before a single claim is made. The clause this
orchestrator looked hardest at — *gaps marked as gaps, never filled with
speculation* — is held, and held in the harder direction:

- Six sources are recorded as **unreached**, each with what it would have told us
  and why the gap is or is not material. **Lu's 1991 dynamic-CDT thesis** (the
  incremental triangulation SURF actually ran on) is known through exactly one
  sentence of Dayan's thesis, and the note says so rather than reconstructing it.
- **TopoR/Eremex is recorded as a gap with the search described** so the next person
  can do better: no algorithmic account was located in any language, and what does
  exist — a vendor trade article claiming "25–40 % less wire length, 2–3× fewer
  vias" — is filed as **a vendor claim carrying no benchmark, baseline or method**,
  not as a measurement. That is the note refusing the exact move the milestone
  forbade.
- Chazelle is marked **second-hand** where it matters rather than cited as if read.

Per technique the template holds: what it is (own words, cited) → aipcb component →
expected gain → runtime risk → verdict. §4 closes with six candidates and a
"Rejected, with reasons, so they are not proposed again" section.

### Gate 2 — the DRAFT exists and is marked DRAFT: PASS

`docs/milestones/m19-DRAFT.md`. DRAFT is in the filename, in the H1, and in a
block-quoted first paragraph stating that nothing is scheduled and no implementation
has begun. It carries M19a–M19d in implementation order, each with a budget against
the **current** (M17) baseline, acceptance criteria, a rejection rule, and a closing
"Decisions required before this becomes a milestone" section. M19c is explicitly
marked **conditional**.

### Gate 3 — no code changed: PASS

`git diff 4a41546..HEAD -- src tests examples` is **0 lines**. The three commits
touch seven files, all under `docs/`: 1 885 insertions. The profiling the note rests
on was driven from scratch scripts outside the repository, M17's precedent, so
nothing under `src/` gained an instrument.

### Gate 4 — report and roadmap: PASS

`docs/reports/m18.md` (285 lines) summarises what was learned, what changed in the
plan and what the owner must decide. The roadmap's M18 entry is closed with its
anchor preserved (three inbound links from `m17.md`), the candidates are in, and two
standing candidates were **re-priced from a primary source** — see below.

### The claim this orchestrator reproduced rather than accepted

The survey's headline is load-bearing for everything downstream, so it was
re-measured independently — a fresh `cProfile` over `route_design` on
`examples/pcie-sata`, written from scratch by the orchestrator, not the subagent's
script:

```
total profiled: 37.4 s
field.py::build_field         cum=  14.435   calls=17
field.py::_via_sites          cum=  12.430   calls=17
geometry.py::geometry_for     cum=  10.702   calls=176
triangulate.py::locate_many   cum=   8.058   calls=68
triangulate.py::_inside       cum=   3.651   calls=3 140 252
triangulate.py::_sign         cum=   1.073   calls=9 426 354
negotiate.py::negotiate       cum=   1.258   calls=1
funnel.py::tighten            cum=   0.039   calls=823
```

**Reproduced to the millisecond.** The funnel algorithm — the thing M16, M17 and the
part-2 plan all called the hot spot — is **thirty-nine profiled milliseconds of a
37.4-second run, 0.13 %**. The `tighten` *stage* really is 93 % of routing and M16
was not wrong about it; almost none of that stage is tightening. It is building the
free space, triangulation and via sites that tightening happens against, and then
throwing them away for the next connection.

The note's own caveat is the reason this is trustworthy rather than merely
impressive: `cProfile` roughly doubles the run (19.3 s unprofiled vs 37.4 s) and
does so unevenly, so every Python row is an upper bound and every single call into
GEOS a lower one — and the note states which side of that line each conclusion
depends on. The 0.13 % figure survives the caveat in the safe direction.

### Findings worth carrying

- **Dayan's 1997 thesis was obtained in full, 161 pages** — the toporouter note's
  largest single gap, closed. Not by access: **every live route is still shut**
  (ProQuest wants an institution, Semantic Scholar returns empty, Google Books has
  metadata only). It came from a 2017 Internet Archive snapshot of a PDF the live
  site no longer serves. The note says plainly that the second attempt succeeded by
  archaeology and that the copy could disappear again.
- **Two standing candidates were re-priced downward from that primary source.** C3's
  detour pass had been carrying 7–16 % from a two-board second-hand figure; Dayan's
  own ROAR measurement over ten bins and 427 branches puts the same mechanism at
  **~3.4 % of wire length**, and M17b already took its named customer away for 0.6 s
  rather than +30 % of the corpus. C5 lost ground twice. A survey that makes the
  project's own backlog *smaller* is the format working.
- **M17c's open question is answered (b), with the noun corrected.** Not "incremental
  re-tightening" — there is nothing worth making incremental about tightening. What
  must stop being rebuilt is the free space, the triangulation and the via sites.
  (a) algorithm replacement is rejected on measurement: Hershberger–Snoeyink is
  worst-case optimal, it is what `funnel.py` already implements, and it costs 0.13 %.
  (c) a compiled kernel is rejected as posed — the top cost is already C — but one
  narrow piece survives, because `_inside` runs 3.1 M times and `_sign` 9.4 M times
  in a scalar Python loop that M17c stopped one function short of. **Constant-factor
  work is not exhausted**, which is the opposite of what everyone expected to hear.
- **The M3 measurement is the one that makes M19c arguable at all**, and it was taken
  rather than assumed: on `pcie-sata`'s F.Cu, a thousand-triangle triangulation is
  built from scratch **137 times because a median of two obstacles out of five
  hundred changed**. Its two caveats are stated as things a candidate must survive —
  the sequence is not monotone (84 obstacles *disappear*, so insertion-only
  incrementality is not enough) and the obstacle geometry differs per rule pair (six
  maintained structures on that layer, not one).
- **§2 is honestly blocked and says so.** Every negotiated-congestion candidate is
  gated on a board that does not converge in one iteration, and no such board exists
  in the corpus. NCTU-GR — whose two-stage cost function is precisely what §2 set out
  to mine — stayed closed, so the section names what it could not get rather than
  padding with what it could.

## Chain end

Both milestones passed. **The chain ends here by design**, not by failure: M18's
second deliverable is a draft for the owner, and starting M19 on unreviewed research
candidates is the improvisation this project does not do. Final report:
[`chain-m17-m18.md`](chain-m17-m18.md).

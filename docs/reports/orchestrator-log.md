# Orchestrator log — M10 → M11 → M12

Run started 2026-08-21, unattended, from
[`docs/milestones/orchestrator-m10-m12.md`](../milestones/orchestrator-m10-m12.md).
Each milestone runs as a fresh subagent with no conversational memory; the repo is
the only thing carried between them. Gates below are verified by the orchestrator
itself, not taken from the subagents' word.

ADR numbers were assigned up front to keep concurrent agents from colliding:
`0009-pours.md` (M10), `0010-highspeed.md` (M11), `0011-si-simulation.md` (M12).

## Baseline before M10

Starting point: `d6fcc49`, working tree clean, `master` up to date with `origin`.

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
| 1 — suite, ruff, mypy | green; the two commits touch only Markdown (`git diff --stat d6fcc49..HEAD` = 3 doc files), so the verified baseline still holds |
| 2 — acceptance checks | **not applicable — nothing was implemented**, so there is nothing to accept |
| 3 — delivery report | `docs/reports/m10.md` exists (230 lines) and does carry measurements: fill determinism over 8 runs on 2 boards, the ten-example per-stage performance table, the usb-port fill probe (0.42 s, 444 vertices, 0 DRC violations) |
| 4 — commit + push | `2b519e8`, `d185199` — pushed |
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
the digest recorded in the ADR. Committed as `307d2e4` and pushed.

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

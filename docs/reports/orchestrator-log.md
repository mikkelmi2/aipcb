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


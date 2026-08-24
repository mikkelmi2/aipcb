# Chain report — M17 → M18

An unattended run of two prepared milestones, 2026-08-24, in the pattern the M10–M12
chain established: a fresh subagent per milestone with no conversational memory, the
repository as the only thing carried between them, and every gate re-run by the
orchestrator rather than taken from the subagent's word. The
[orchestrator log](orchestrator-log.md#orchestrator-log--m17--m18) has the gate
detail; this is the summary.

Starting point `6b6a963`, ending point `7baf3f9` plus this report. Nothing halted.

**Both milestones passed every gate. The chain ends after M18 by design** — M18's
second deliverable is a *draft* milestone, and implementing unreviewed research
candidates is exactly the improvisation this project does not do. What the owner
needs to review is at the bottom.

---

## M17 — the measured candidates

Three candidates landed, none rejected. **Corpus routing time fell from 61.863 s to
29.476 s (−52 %)**, and it did so with **ten of eleven board hashes byte-identical to
M16** — the speed cost nothing in what the router decided. On the two slowest boards
the tightening stage fell **−55.6 %** (`pcie-sata`, 33.883 → 15.035 s) and **−52.5 %**
(`mcu-4layer`, 8.553 → 4.063 s) against a budget of ≥ 25 %. The via pass gave back
**4 vias and 30.285 mm of copper** for **+1.7 %** runtime against a +10 % budget, and
the E2 retrace on `pcie-sata` — the defect M16's own guard found on the day it
landed — is **gone**, verified here by routing the board through the CLI and
confirming `route-doubles-back` is absent from its fourteen diagnostic codes, for
**+1.3 %** against a +5 % budget. The guard stayed in the suite, inverted to assert
zero rather than deleted. The new capability-ladder number is **11.3 minutes for a
900-connection board**, down from 23.7 — and the honest note attached to it is that
the previously cited "≈ 31 min" does not reproduce from the committed baseline by
M16's own stated method, so the comparison published is the pair that does.

Two findings outlived the numbers. **M16's "37 of 90 layer changes made where no
corridor was half full" overstated the opportunity by an order of magnitude**: of 55
candidate spans corpus-wide, 2 collapsed — 21 rejected because the on-layer route is
*longer*, 32 because the free space is split, and **zero on capacity**. This router's
vias are, with four exceptions, load bearing, and the capacity arbiter built to
adjudicate them **never fired**, which the report states rather than dressing up as a
win. And the profile that produced the −52 % found the time was not where the stage
name said: two thirds of "tightening" was the congestion field being rebuilt per
repaired connection, 1.9 M Shapely `Point` constructions on one board. **No algorithm
changed** — the log-log exponents moved 1.72 → 1.70 and 1.12 → 1.10, which is the
evidence that M17 did not answer the question M18 was for.

*Deviations flagged:* M17a and M17b turned out to be **one mechanism** (every retrace
in the corpus *is* a via pair returning to its own layer), so no separate trimmer was
written; the controlled-impedance exclusion is **by class**, since the corpus cannot
verify it and the test hands the guard a synthetic candidate to refuse; one board
hash moved and no golden did. The orchestrator corrected one scoreboard cell that
read +1.6 % where the report's own §2.4 derives +1.3 % — inside budget either way,
but two statements of one measurement disagreeing is the failure mode these reports
exist to prevent.

## M18 — the literature survey

Research only: `git diff` over `src/`, `tests/` and `examples/` across its three
commits is **empty**, and 1 885 lines landed under `docs/`.

**The survey's headline is that the question three milestones have been asking was
aimed at the wrong function.** `funnel.py::tighten` — the tightening algorithm
itself, the thing the part-2 plan called the hot spot — costs **0.039 profiled
seconds of a 37.4-second route, 0.13 %**. This orchestrator reproduced that from a
freshly written profile script and got it to the millisecond. The `tighten` *stage*
genuinely is 93 % of routing and M16 was not wrong; almost none of that stage is
tightening. It is building the free space, the triangulation and the via sites that
tightening happens against, and discarding them for the next connection. On
`pcie-sata`'s F.Cu a thousand-triangle triangulation is rebuilt **137 times because a
median of two obstacles out of five hundred changed**.

So **M17c's open question is answered (b)** — but with the noun corrected. There is
nothing worth making incremental about tightening; what must stop being rebuilt is
the geometry. **(a) algorithm replacement is rejected on measurement**:
Hershberger–Snoeyink is worst-case optimal for this problem, it is what `funnel.py`
already implements, and it costs 0.13 %. **(c) a compiled kernel is rejected as
posed** — the largest single cost is already C — while one narrow piece of it
survives, because `_inside` runs 3 140 252 times and `_sign` 9 426 354 times in a
scalar Python loop that M17c stopped one function short of. Constant-factor work is
**not** exhausted, which is the opposite of what this chain expected to hear.

**Dayan's 1997 thesis was obtained in full, 161 pages** — the toporouter note's
largest gap, closed. Not by access: every live route is still shut, and it came from
a 2017 Internet Archive snapshot of a PDF the live site no longer serves. The note
says plainly that the second attempt succeeded by archaeology and that the copy could
vanish again. Reading it made the project's own backlog **smaller**: candidate C3's
detour pass, carried at 7–16 % on a second-hand two-board figure, is re-priced to
**~3.4 %** by Dayan's own ROAR measurement over ten bins and 427 branches — while
gaining the one structural argument it lacked, his §6.1 proof that some topologies
are unreachable by *any* net ordering. C5 lost ground twice over.

*Gaps, marked as gaps.* Six sources stayed closed and none is summarised from its
title. **NCTU-GR**, whose two-stage cost function is exactly what §2 set out to mine,
was unreachable — so §2 names what it could not get instead of padding. **No
algorithmic account of TopoR exists in any language that could be found**, and the
vendor claim that does exist ("25–40 % less wire length, 2–3× fewer vias") is filed
as a claim carrying no benchmark, baseline or method. §2 is also honestly blocked at
the other end: every negotiated-congestion candidate needs a board that does not
converge in one iteration, and **no such board exists in this corpus**.

---

## What the owner needs to review

**The deliverable of this chain is a decision, not a merge.**
[`docs/milestones/m19-DRAFT.md`](../milestones/m19-DRAFT.md) is a draft and is marked
as one in its filename, its title and its first paragraph. It proposes, in order:
**M19a** vectorise the point-location tail (−10 % corpus routing, all eleven hashes
byte-identical), **M19b** bound the private repair field (−25 % further), **M19c** an
incremental free-space mesh (−30 % further, **conditional**), **M19d** re-fit the
extrapolation and see whether the exponents finally move.

Five decisions, and the first is the one that matters:

1. **Is M19c in or out?** It is the only item in this project's history that would
   partially reverse an ADR and put a **hand-owned geometric predicate into the
   router** — the exact failure mode that killed the gEDA toporouter this project
   wrote an autopsy of. The survey recommends gating it on M19a's and M19b's measured
   results rather than deciding now. The measurement that makes it arguable at all
   (137 rebuilds for a median of two changed obstacles) comes with two caveats a
   candidate must survive: the obstacle sequence is **not monotone**, so
   insertion-only incrementality is insufficient, and the geometry differs per rule
   pair, so it is six maintained structures per layer rather than one.
2. **Does the congestion stress board come first?** Every §2 candidate is blocked on
   it and it is already a graduation condition. If it goes first, M19 shrinks to
   M19a/M19b.
3. **Does the `pcie-sata` fab round gate this?** M19a is hash-preserving by its own
   acceptance criterion and is arguably exempt; M19b and M19c are not.
4. **Does C3 survive its re-pricing?** ~3.4 % copper for a +30 % runtime budget, now
   measured from a primary source instead of estimated from a secondary one.
5. **Is a second attempt worth making on the three closed sources?** Only Lu's 1991
   thesis would change M19c's design, and only marginally.

Three smaller items M17 left open, none of which blocked M18:

- `ROUTER_STAGES` gained `reclaim` and `route all --json` gained
  `routing.reclaimed` — deliberate **public interface additions** that want
  confirming as permanent.
- `docs/images/*` are stale by four vias on the flagship board. Left alone:
  regenerating them is large binary churn for an invisible difference, and when to
  spend it is the owner's call.
- **The capacity arbiter has no real-board evidence.** It is built and unit-tested
  and the corpus never gave it a span to reject. If a denser example is ever added,
  it is the first thing to re-measure.

## Verification, by the orchestrator

| Check | Result |
|---|---|
| `pytest -q` | exit 0 — 1 332 tests, 2 `AIPCB_FULL_CORPUS` skips |
| `ruff check .` | clean |
| `mypy --strict src` | clean, 96 source files |
| `bench --compare` vs the M16 baseline | exit 0, no regressions; every landed candidate inside its stated budget |
| E2 on `examples/pcie-sata` | `route-doubles-back` absent from a CLI route run here; guard retained and inverted |
| M18 code diff | empty over `src/`, `tests/`, `examples/` |
| `funnel.py::tighten` = 0.13 % | reproduced from an independently written profile script |

All six commits are pushed to `origin/master`.

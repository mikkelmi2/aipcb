# M19 review — what the owner changed between draft and milestone

M18's second deliverable was a *draft* milestone with five open decisions at the
bottom, and the [M17→M18 chain](chain-m17-m18.md) halted there by design: the
deliverable of that chain was a decision, not a merge. This is the record of the
review. **No implementation code moved in this session** — `git diff` over `src/`,
`tests/` and `examples/` is empty across its commit, exactly as M18's was.

What went in: `docs/milestones/m19-DRAFT.md`.
What came out: [`m19-incremental-geometry.md`](../milestones/m19-incremental-geometry.md),
[`m20-placement-quality.md`](../milestones/m20-placement-quality.md), a rejection
recorded in the [postmortem note](../notes/toporouter-postmortem.md) §C, roadmap
updates, and regenerated images.

## The five decisions, and what each one changed

### 1. M19a and M19b — approved as drafted

**Decision: approved.** Incremental geometry is what the measurements unambiguously
point at — 137 rebuilds of a thousand-triangle mesh for a median of two changed
obstacles, and `_inside` running 3 140 252 times in a scalar Python loop.
Hash preservation stays a **hard requirement** for M19a and the per-stage budgets
stand as drafted.

**What changed in the text.** Very little, and deliberately so. Two clarifications:

* M19a's acceptance now says hash preservation is **the criterion, not a hope** — a
  version of M19a that is faster and moves a coordinate is rejected rather than
  argued for. The draft implied this; the milestone states it, because "expected to
  hold" and "must hold" are what separate M19a from M19b and the difference has to
  survive being read quickly.
* "all eleven board hashes" became "all board hashes", because decision 5 adds a
  twelfth board. See the budget note below, which is the one substantive addition.

### 2. M19c — the gate approved, the measure not yet

**Decision: the conditional gate stands as the survey recommended.** M19c is decided
by M19a's and M19b's measured results, not now. **Two conditions were added** that
must hold if it ever activates:

1. **The hand-owned predicate ships with property-based testing** — `hypothesis`,
   against shapely as the oracle, **explicitly fuzzing the degenerate cases**:
   collinear, coincident, near-epsilon. Those are the cases that killed the
   toporouter, and they are the cases uniform random sampling almost never
   generates. A property test that only ever sees general position has tested
   nothing that matters.
2. **A kill-switch config falls back to the robust path.** The rebuild path is not
   deleted when the incremental path lands; the predicate is **opt-in** until it has
   survived the full corpus *plus* the stress board with byte-identical output, and
   the report names which boards that was.

**Two practicalities are written into the milestone** so they are not discovered
late: `hypothesis` is **not** a dependency of this project today, so adding it is
part of M19c's cost rather than a free assumption; and a randomised test generator
has to be reconciled with the determinism bar, so the property tests run
derandomised or with the seed pinned, with any exploratory randomised run kept as a
separate non-gating step. A flaky test in a repository whose central promise is
byte-stability costs more than it finds.

**The gate text now carries the postmortem's root-cause finding inline**, quoted
rather than linked, so the rationale travels with the decision:

> A topological router is a *geometry* program wearing a graph program's clothes.
> Its correctness rests entirely on predicates over floating point [...] and every
> one of the fatal failures was a predicate that stopped being true. The routing
> search, the rip-up policy, the cost model: none of them ever failed.

with the concrete case named underneath it — bug #846789, nested
`swap_if_in_circle()` calls roughly ten thousand deep on a board with two resistors,
after **somebody else** changed PCB's base units to nanometres. **The predicate did
not change; its inputs did.** That is the sentence the two conditions exist to
answer, and it is why a milestone that reads only the gate still meets the argument.

### 3. C3 — rejected, with its numbers

**Decision: rejected.** 3.4 % expected copper for +30 % runtime fails any budget
this project has stated. The candidate is removed from the milestone and recorded as
a rejection in the [postmortem note](../notes/toporouter-postmortem.md#c3-a-detour-pass--rejected-at-m19)
§C, per the closure rule — the rule M16 wrote precisely so that a measured negative
result is met by the next person who proposes the same thing.

The numbers as recorded:

| | Value |
|---|---|
| Expected gain | **~3.4 % of wire length** — Dayan's ROAR optimiser, ten two-layer bins, 427 branches, detour 8.84 % → 5.18 % |
| Runtime budget | **+30 %** of corpus `router_seconds` |
| Superseded figure | 7–16 %, Blake's, on **two** boards, second-hand |
| Named customer | **gone** — the `route-doubles-back` retrace was fixed at M17b for **+1.3 %** on that board |
| Corroboration against | **53 of 55** collapsible spans corpus-wide unimprovable at M17a; **zero** rejected on capacity |
| What survives in its favour | Dayan §6.1 — some topologies are unreachable by *any* net ordering |

**Dayan §6.1 is accepted and does not rescue the candidate.** It establishes that
*something* post-convergence is needed for completeness on some topologies, not that
**this** post-pass is worth 30 % of the corpus's routing time. That argument is
written into the rejection as the one thing that could reopen the question: a
cheaper mechanism addressing the same structural gap would be a **new** candidate,
budgeted afresh, and not this one. Recording that distinction is the difference
between a rejection and an amnesia.

The roadmap's candidate table row is struck through and carries the same numbers, so
a reader who only ever opens the roadmap meets the rejection where the candidate
used to be.

### 4. Placement — evaluated now, and split out as M20

**Decision: evaluate now; the author's judgment on whether to split. It is split**,
as [`m20-placement-quality.md`](../milestones/m20-placement-quality.md).

**Why split rather than fold in.** The draft was already 232 lines across four
candidates in one subsystem, and decision 5 adds a fifth item to the front of it.
Placement is a *different subsystem* with a different judge-of-record, a different
ADR (0008, not 0006), and — under decision 6 — a different position in the fab
queue. A milestone called "incremental geometry" that also rotates components has
stopped naming what it does. Decision 6's own wording (*"any M20 placement
implementation"*) anticipated the split.

**"The evaluation happens now, not later" is honoured**: M20 is written, budgeted
and sequenced in this session, not deferred to a future decision. What is deferred
is only its *implementation*, and only by decision 6's fab ordering.

M20 as written:

* **M20a rotation optimisation** — four orthogonal orientations per movable
  component, scored against pin-to-net direction, near-zero runtime by construction
  (four evaluations per part, once, no search, no iteration). Budget **≤ +2 %**
  build wall clock. The expected crossing and wirelength reduction is written down
  as **a hypothesis with a mechanism, to be measured** — not as a benefit.
* **M20b routability-estimated positioning** — a congestion estimate in the packing
  cost, using the same cut model `check_capacity` uses so the placer's prediction
  and the router's report are the same quantity. Budget **≤ +20 %**. Gated on M20a.
* **M20c the validation experiment**, promoted to its own acceptance line as the
  decision asked: re-run `aipcb bench` after placement improvements and measure
  which router-level symptoms — wirelength, vias, crossings, iterations,
  `headroom_mm` — move **with zero router changes**. If they move, the thesis that
  the remaining gains are upstream is measured rather than argued, and it re-prices
  C1 and C4, whose value is partly a function of how bad the placement they inherit
  is. If they do not, that is a negative result of the first importance and it goes
  into the postmortem note under the same closure rule as C3.
* **ePlace/RePlAce and force-directed methods** are recorded as literature pointers
  for a survey *if M20a's measurement suggests one is warranted* — named so a future
  session starts from names rather than a search box, and explicitly not scheduled.

The rationale the decision asked to be included is the milestone's opening argument:
M17a found the via opportunity **ten times smaller** than M16's proxy suggested (two
collapsible spans of 55, not 37 of 90) and M17c halved the runtime. The levers
*inside* the router keep measuring smaller than they looked.

### 5. The stress board — moved to the front of M19

**Decision: moved early.** It is now **M19s**, the first item in the milestone.

**Lettering.** It is `s`, not `a`, on purpose: M19a–M19d keep the names the draft,
the roadmap, the [M18 report](m18.md) and the [chain report](chain-m17-m18.md)
already use. Renaming four candidates to insert one at the front would silently
invalidate every cross-reference written in the last two days, which is a worse
failure than an out-of-order letter.

**Four reasons are recorded, each independent of the others**: incremental geometry
should be proven on the largest thing that exists; the capability ladder's 11.3 min
@ 900 connections is an extrapolation from eleven boards none of which is near 900,
and deserves an anchor; every §2 candidate (L3, L4) is blocked on a board that does
not converge in one iteration; and the capacity arbiter has never once fired on real
data.

**The multi-sheet linkage is recorded in both directions**, as the decision asked.
The roadmap's [Schematics](../roadmap.md#schematics) section says multi-sheet output
is *"blocked on a design, not on machinery"* — M14a was not built because the
largest bundled example has 12 components. M19s is very likely the first design in
this repository big enough to justify splitting a sheet. The milestone states
explicitly that **M19 does not build multi-sheet output and must not**, and asks
M19s's author to give the board a module structure that would survive being split.

**One consequence had to be handled, and it is the substantive addition to the
milestone.** Every budget in M19 is quoted as a percentage of *corpus*
`router_seconds`, and M19s changes the corpus from eleven boards to twelve. The
milestone now carries a short section saying so: M19s refreshes the baseline in its
own commit and publishes corpus totals before and after, so the step is visible and
is not mistaken for a regression; M19a/M19b/M19c percentages are against the
**refreshed twelve-board baseline**; and every candidate additionally reports **per
board**, because a candidate that helps only the new stress board is a different
result from one that helps the corpus evenly and a single corpus percentage hides
which. M19d likewise publishes its fit **twice** — over eleven boards for line-by-
line comparability with M16's and M17's exponents, and over twelve for the better
fit — since quoting a twelve-board fit against M17's eleven-board fit would compare
two different measurements.

## The smaller items

* **`reclaim` / `routing.reclaimed`: confirmed permanent, marked beta.** They were
  flagged by M17 as public-interface additions wanting confirmation and had been
  documented nowhere outside the reports. They now have a section in
  [the guide](../guide.md#what-the-via-pass-took-back--routingreclaimed-beta) with
  the real JSON output, the statement that both are permanent interface rather than
  leaked implementation detail, and what **beta** means for them concretely: the key
  is stable, the shape may still gain fields, and a pre-M17 bench result simply has
  no `reclaim` key — which `--compare` already handles by deriving its columns from
  the data. The section also records that `rejected.capacity` is **0** on every
  bundled example, so the arbiter's never having fired is documented where a user
  meets it rather than only in a report.
* **`docs/images/*`: regenerated**, in this session's commit. Details below.
* **Closed sources: one retry authorised as a background task** in any future
  session, not a dedicated effort. Written into the roadmap's gaps paragraph, with
  the note that nothing is blocked on it and that only Lu's 1991 thesis would change
  any design here, marginally, since Shewchuk & Brown and Livesu et al. supersede
  it.
* **Fab sequencing: explicit in the roadmap.** The part-2 sequencing paragraph now
  carries a numbered order — (1) M19s/M19a/M19b, (2) BOM/CPL export milestone then
  the fab round, (3) M19c and any M20 placement implementation, after the board is
  ordered — with the reason attached to each step rather than left to inference.

## The images

`tools/make_images.py`, no arguments, so the simulation plot was drawn from the
cached `out/si` results rather than re-solved. All five were regenerated;
**two changed**:

| Image | Result |
|---|---|
| `02-schematic.png` | byte-identical |
| `03-placed.png` | byte-identical |
| `04-routed.png` | **changed** — the four vias M17a reclaimed on `pcie-sata` |
| `05-simulation.png` | byte-identical |
| `06-3d.png` | **changed** — the same four vias, raytraced |

The chain report called them *"stale by four vias on the flagship board"* and that
is exactly what the diff is. Three of five coming back byte-identical is the useful
part: the schematic, the placement and the simulation results have not moved, so the
churn is confined to what actually changed.

Two notes for whoever runs this next. **`examples/pcie-sata/pcie-sata.kicad_pcb` was
rewritten by the script and came back byte-identical** — `make_images.py` runs
`aipcb build --fresh` and `aipcb route all` on the flagship, and git reported no
change to the board, which is `test_routing_is_byte_stable`'s promise being kept
outside the test suite. And **the simulation step failed once and succeeded on
retry**, with `RuntimeError: FT_Render_Glyph ... failed with error 0x62: raster
overflow` from matplotlib 3.11.1's Agg backend — a flaky FreeType rasterisation
failure, not a change in the data; the retry produced a byte-identical PNG.

## What was not changed, and why

* **No implementation.** This was a revision session: documentation and planning
  only, as scoped.
* **No new ADR.** M19c's ADR is a *precondition of M19c running*, and M19c is
  conditional on measurements that do not exist yet. Writing it now would be
  deciding the thing the gate exists to leave open.
* **The M18 draft's own text is preserved where it was right.** M19a's and M19b's
  bodies, the rejection rule, the guardrails and the "not in this milestone" list
  are carried across substantially as written. A review that rewrites what it
  approves makes it harder, not easier, to see what the review changed.
* **Historical reports were not rewritten.** [`m18.md`](m18.md) §4 and the
  [chain report](chain-m17-m18.md)'s handover section still ask the five questions
  as they asked them; each gained a note at the top saying they are answered and
  pointing here. What a chain handed over is part of its record.

## Verification

| Check | Result |
|---|---|
| `pytest -q` | exit 0 — **1 332 tests**, 2 `AIPCB_FULL_CORPUS` skips |
| `ruff check .` | clean |
| `mypy --strict src` | clean, 96 source files |
| `git diff` over `src/`, `tests/`, `examples/` | empty — documentation and images only |
| `examples/pcie-sata/pcie-sata.kicad_pcb` after a full re-route | byte-identical |
| `aipcb route all --json` `.routing.reclaimed` | reproduced; the guide quotes the real output |
| `docs/images` regenerated | 5 of 5 produced, 2 changed, 3 byte-identical |
| Internal links | 0 broken of **520** relative links and anchors across every `.md` in the repository; every `m19-DRAFT.md` reference retargeted or de-linked, and the C3 anchor updated in all three files that use it |

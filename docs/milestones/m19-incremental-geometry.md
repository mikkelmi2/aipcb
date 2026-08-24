# Router improvements, part 3: the stress board, and the geometry that gets rebuilt (M19)

M17 spent the evidence M16's baseline produced and halved the corpus's routing
time. M18 read the literature and found that the question everybody has been asking
— *is the tightening algorithm the right shape?* — was aimed at the wrong function.
[`docs/notes/routing-literature.md`](../notes/routing-literature.md) has the whole
argument; the one number is this:

> **`funnel.py::tighten` costs 0.039 profiled seconds of a 30-second route.**
> The `tighten` *stage* is 93 % of `examples/pcie-sata`'s routing time. Almost all
> of that stage is building — and discarding — the free space, triangulation and via
> sites that tightening happens against.

So this milestone is not about tightening. It is about the three places the router
constructs the same thing twice, in increasing order of cost and risk — and it
begins by building the board those three things should be measured on.

> **Approved 2026-08-24**, from the draft M18 wrote for the owner's review. The
> owner's five decisions, and what each one changed between draft and milestone,
> are recorded in [`docs/reports/m19-review.md`](../reports/m19-review.md). The
> summary: M19a and M19b approved as drafted, M19c's conditional gate approved
> with two hard conditions added to it, the congestion stress board moved to the
> front, candidate C3 rejected outright rather than carried, and placement split
> out as [M20](m20-placement-quality.md).

## Session context (fresh session)

Read [`docs/notes/routing-literature.md`](../notes/routing-literature.md) — §1.6 is
the argument this milestone implements and §4 is where L0, L1 and L2 are scoped —
then [`docs/reports/m17.md`](../reports/m17.md) §3 for what M17c already did, and
[`bench/results/README.md`](../../bench/results/README.md) for the rule that the
baseline moves deliberately. The committed baseline is at commit `3b59a52`
(corpus `router_seconds` **29.476**, copper **3 897.150 mm**, vias **81**, 275/278
routed, `pcie-sata` `router_seconds` **16.196** of which `tighten` **15.035**).

### Before writing code

Per [CLAUDE.md](../../CLAUDE.md), and because M18's own profile contradicted three
milestones of received framing:

1. **Re-take the profile at this milestone's own HEAD**, on `pcie-sata` and
   `mcu-4layer`, from a scratch script outside the repository (M17's precedent — no
   timer goes into `src/`). M18's table is at HEAD `4a41546` on 2026-08-24 and is
   the comparison, not the assumption.
2. **Record both the profiled and the unprofiled wall time in the same run.**
   `cProfile` roughly doubles this workload (19.3 s wall against 37.4 s profiled)
   and it inflates functions with millions of calls far more than single calls into
   GEOS. Every budget below is stated against `bench`'s unprofiled
   `router_seconds`, never against profiled seconds, for that reason.
3. **Split `geometry_for`'s cost** between the GEOS set operations
   (`union_all`, `difference`) and `constrained_delaunay_triangles`. M18 measured
   1.32 s and 4.30 s profiled on one board. **M19c's whole value depends on that
   split**, because an incremental mesh replaces only the second unless the set
   operations go too.

### What the budgets are measured against, now that the corpus grows

M19s adds a board to the corpus, so the corpus totals every budget below is quoted
against **change on the day M19s lands**. The rule is:

* M19s refreshes `bench/results/baseline.json` in its own commit, and its report
  states the corpus totals before and after the new board, so that the step from
  eleven examples to twelve is visible and is not mistaken for a regression.
* **M19a's, M19b's and M19c's percentages are against the refreshed twelve-board
  baseline**, not against the 29.476 s above.
* Every candidate additionally reports **per board**. A candidate that helps only
  the new stress board, or only the eleven that were already there, is a different
  result from one that helps the corpus evenly, and a single corpus percentage
  hides which.

## M19s — the congestion stress board (first)

*Lettered out of sequence on purpose: M19a–M19d keep the names the draft, the
roadmap and the [chain report](../reports/chain-m17-m18.md) already use.*

A **congestion stress example** harder than `examples/congestion` — more nets than
channels, on an outline with no slack — either routes or hands over cleanly. It is
already a [graduation condition](../roadmap.md#maturity-and-graduation) and it is
the first thing this milestone builds, for four reasons that are independent of
each other:

* **Incremental geometry should be proven on the largest thing that exists.** M19a,
  M19b and M19c are all constant-factor or structural wins over rebuild cost, and
  rebuild cost is what grows with board size. Measuring them on a corpus whose
  largest board is `pcie-sata` measures them where they matter least.
* **The capability ladder is an extrapolation with no anchor at its own top end.**
  M17d's log-log fit predicts **11.3 minutes for a 900-connection board**, from
  eleven examples none of which is close to 900. One real data point at the top of
  the range is worth more than another decimal place on the exponent.
* **Every §2 candidate is blocked on a board that does not converge in one
  iteration.** Ten of eleven examples converge in one and `enclosure` in four. L3
  (virtual capacity) and L4 (the Gompertz anneal and rip-up ordering) cannot be
  evaluated at all until such a board exists; this is the board.
* **The capacity arbiter has never fired.** M16a's special cuts were built so a via
  collapse could be tested against a sound cut model, and across the whole corpus
  M17a found *not one* span rejected on capacity — length was binding everywhere.
  A denser board is exactly where that arbiter would finally speak.

**It is a design task, not a router task**, and that is why it is separable from
everything below it. What it must produce:

* an example under `examples/`, in the source format, with the same licence and
  provenance discipline as the rest of the corpus;
* **more nets than channels on at least one cut**, verifiable from
  `check_capacity`'s own report rather than asserted in prose;
* an honest outcome recorded either way — it routes, or it hands over cleanly with
  the diagnostics naming what it could not do. **A stress board that quietly routes
  first time has failed to be a stress board**, and the report says so and makes it
  harder rather than declaring victory.
* its row in `bench/results/baseline.json`, its iterations-to-convergence, and its
  measured `router_seconds` **against what M17d's fit predicted for its connection
  count** — the anchor point, published as a hit or a miss.

**Budget: none on runtime.** This board is allowed to be the slowest thing in the
corpus; that is its function. What it is not allowed to do is be non-deterministic
or fail a guard: `test_routing_is_byte_stable` covers it like any other example.

**It is also the multi-sheet test case.** [Multi-sheet schematic output is blocked
on a design, not on machinery](../roadmap.md#schematics) — M14a was not built
because the largest bundled example has 12 components and the median 7, so any
threshold worth configuring would have shipped unexercised. A board with enough
nets to congest a routing channel is very likely the first design in this
repository big enough to justify splitting a sheet. **This milestone does not build
multi-sheet output**, and must not; it notes the linkage so that whoever does build
it knows the design it was waiting for now exists, and so that M19s's author gives
the board a module structure that would survive being split.

## M19a — Vectorise the point-location tail (survey candidate L0)

`Triangulation.locate_many` batches the R-tree query and then runs a scalar Python
loop: `_inside` **3 140 252 times**, `_sign` **9 426 354 times**, on one board.
M17c vectorised everything around this and stopped one function short.

* Replace the candidate-resolution loop with a vectorised orientation or barycentric
  test over numpy arrays.
* **The tie-break is the whole risk.** Candidates are tried in *ascending triangle
  index* so a point on a shared edge lands in the lower-numbered triangle. A
  vectorised mask does not reproduce that for free; it has to be written to, and
  tested against the scalar implementation on a case that exercises it.
* `locate` (the single-point path, 29 667 calls) must keep giving the same answer as
  `locate_many` — there is an existing docstring promise that they are identical.

**Budget: ≥ 10 % reduction in corpus `router_seconds`, and every board hash
byte-identical.** A performance change that moves a coordinate is a quality change
wearing a disguise (M17c §3.3); this one has no excuse to move anything, because it
computes the same predicate. **Hash preservation is a hard requirement here, not an
expectation** — it is the criterion, and a version of M19a that is faster and moves
a coordinate is rejected rather than argued for.

## M19b — Bound the private repair field (survey candidate L2)

`_repair` builds an entire whole-board `LayeredField` for **one** connection, and
does it sixteen times on `pcie-sata`. It is **16.30 s of 29.8 s profiled** — the
single largest cumulative cost in the profile — of which `_via_sites` is 12.35 s.
The connection it is repairing usually needs a corridor between two pads that are
millimetres apart.

* Bound the rebuilt field to a region around the failing connection: the two pads'
  bounding box inflated by a margin, expressed in millimetres derived from the
  board rather than as a magic constant.
* **Expansion on failure.** Start bounded; if `search_path` returns nothing, widen
  and retry; the last attempt is the whole board, which is what happens today. That
  makes completion unchanged *by construction* rather than by hope, and it is the
  clause that makes this candidate safe.
* Plane layers still need their via-column obstacles, so the bound is on the
  *region*, not on the *layers*.
* The charge back to the shared field (`_charge`, `plan.py`) must be unaffected: a
  bounded repair still books its demand against the whole-board congestion model.

**Budget: ≥ 25 % reduction in corpus `router_seconds` on top of M19a, at unchanged
completion and unchanged copper.** Board hashes are *expected* to hold but are not
guaranteed — a bounded field can find a different-but-legal corridor. If one moves,
the report explains which board, which connection, and by how much, or the
candidate is rejected.

## M19c — An incremental free-space mesh (survey candidate L1) — **conditional**

**The gate stands as the survey recommended it: this is decided by M19a's and
M19b's measured results, not now.** Do not start it if M19a and M19b together land
more than half of the corpus routing time. The survey's whole ordering argument is
that the cheap candidates take a bigger bite than this one, and if they do, L1's
remaining prize is much smaller than the profile makes it look today.

**Two conditions were added to this gate by the owner, and both must hold if it
ever activates.** They exist because of what
[the toporouter postmortem found as root cause](../notes/toporouter-postmortem.md#b2-root-cause):

> A topological router is a *geometry* program wearing a graph program's clothes.
> Its correctness rests entirely on predicates over floating point — winding,
> in-circle, intersection, tangency — and every one of the fatal failures was a
> predicate that stopped being true. The routing search, the rip-up policy, the
> cost model: none of them ever failed.

The concrete failure that finding is drawn from is
[bug #846789](../notes/toporouter-postmortem.md#b1-what-actually-broke): nested
`swap_if_in_circle()` calls roughly ten thousand deep, on a board with two
resistors, after **somebody else** changed PCB's base units to nanometres. The
predicate did not change; its inputs did. M19c would put a predicate of exactly
that kind into this router, so:

1. **The hand-owned predicate ships with property-based testing against shapely as
   the oracle.** `hypothesis`, generating geometry, comparing the hand-owned
   predicate's verdict against the robust library's on every generated case, and
   **explicitly fuzzing the degenerate classes** — collinear points, coincident
   points, and points within an epsilon of an edge or of each other. Those are the
   cases that killed the toporouter and they are the cases uniform random sampling
   almost never produces. A property test that only ever sees general position has
   tested nothing that matters.

   Two practicalities, so they are not discovered late. **`hypothesis` is not a
   dependency of this project today** — `[project.optional-dependencies] dev` is
   `pytest`, `ruff`, `mypy`, `types-PyYAML` — so M19c adds it, and that is part of
   the candidate's cost rather than a free assumption. And **a randomised generator
   has to be reconciled with this project's determinism bar**: the suite must not
   pass on one machine and fail on another for reasons of seed. Run the property
   tests derandomised, or with the seed and example database pinned, and keep any
   exploratory randomised run as a separate, explicitly non-gating step. A flaky
   test in a repository whose central promise is byte-stability costs more than it
   finds.
2. **A kill-switch config falls back to the robust path, and the predicate is
   opt-in until it has survived the full corpus plus the stress board.** The
   rebuild path is not deleted when the incremental path lands; it stays reachable
   by configuration, so that a board which trips the predicate in the field has a
   documented way back to a correct answer. The default flips to incremental only
   after the whole corpus **and M19s's stress board** have run through it with
   byte-identical output, and the report says which boards that was.

If it is started, it is the largest and riskiest thing this project has attempted
in the router:

* Maintain, per (layer, net-rule) pair, a persistent CDT of free space with
  adjacency, and apply new copper by segment insertion rather than rebuilding.
* **Deletion is not optional.** M18 measured 84 obstacle disappearances on
  `pcie-sata`'s F.Cu alone; the sequence is not monotone. Either constraint removal
  (Kallmann, Bieri & Thalmann 2003) exists, or a rebuild is the documented fallback
  for that case, and which one is chosen is stated up front.
* **There are up to six distinct `(clearance, track_width)` rule pairs per layer**
  on `pcie-sata` (M18 measurement M3), so this is several maintained meshes per
  layer, not one. A budget that assumes one mesh per layer is wrong.
* Implement from the papers, never from anybody's source: Shewchuk & Brown 2015 for
  insertion, **Livesu, Cherchi, Scateni & Attene 2022** for the deterministic
  linear-time cavity fill with exact predicates, Kallmann et al. 2003 for removal.
* **It needs its own ADR**, because it partially reverses
  [ADR 0006](../decisions/0006-routing-approach.md)'s refusal to hand-roll a CDT.
  That ADR must weigh what has changed since: aipcb already snaps to `_QUANT = 1e-6`
  mm so its coordinates are on an integer lattice, which gCDT (2026) reports as what
  makes orientation and in-circle decisions deterministic; Livesu et al. supply a
  correctness proof this project can hold; and there are invariants and
  byte-stability tests the toporouter never had. It must also weigh what has not
  changed: [the toporouter died of exactly this](../notes/toporouter-postmortem.md#b1-what-actually-broke)
  — nested in-circle flips ten thousand deep after somebody changed the coordinate
  units.
* **Scale robustness stops being optional.** [E3](../notes/toporouter-postmortem.md#e3-scale-robustness-is-untested)
  has been a flagged exposure since the toporouter study. If this milestone runs, E3
  is a prerequisite and not a nice-to-have: a board translated far from the origin
  and scaled must produce the same topology, before a hand-owned predicate ships.

**Budget: ≥ 30 % further reduction in corpus `router_seconds`, byte-identical board
hashes, and `mypy --strict` clean over the new module.** Anything less and the
rebuild stays — a 10 % win is not worth owning a predicate.

## M19d — Re-fit the extrapolation

Re-run M17d's method verbatim — log-log least squares of `router_seconds` against
connections attempted, and against connections × cuts, over the whole corpus — and
publish the new numbers alongside M16's (1.72 / 1.12) and M17's (1.70 / 1.10).

**Two things make this fit more interesting than the last one.** M19s adds a point
at the top of the range, where a power law is actually constrained; and M19b changes
the *shape* of the repair cost from whole-board to bounded, while M19c would change
it from rebuild-per-connection to update-per-connection. M17c removed constant
factors and the exponents did not move. If they still do not move after a shape
change, that is a finding worth as much as the runtime.

**Publish the fit twice**: over the eleven original examples, so it is comparable
with M16's and M17's numbers line for line, and over all twelve, which is the
better fit and the one to carry forward. Quoting only the twelve-board fit against
M17's eleven-board fit would be comparing two different measurements.

## Rejection rule

Unchanged from part 2, and it applies to every candidate above: **a candidate that
misses its budget or regresses quality is rejected, with its numbers written into
[`docs/notes/routing-literature.md`](../notes/routing-literature.md) §5** — the note
whose §4 proposed it — rather than deleted or left in a branch. A negative result
nobody writes down gets proposed again.

M19c carries one extra rejection clause of its own: **if the ADR cannot be written
honestly, the candidate is rejected without being built.** "We think the predicates
will be fine" is not an ADR.

## Sequencing, and the fab round

The [part-2 rule](../roadmap.md#part-2-the-quality-candidates-and-the-rule-they-are-held-to)
put quality work after the `pcie-sata` fab round by decision rather than by
dependency, so that the copper measured and the copper produced are the same
copper. The owner has settled how that applies here:

1. **M19s, M19a, M19b** — this milestone's hash-preserving and
   completion-preserving work. M19a is hash-preserving by its acceptance criterion;
   M19b keeps completion and copper by construction and explains any hash that
   moves.
2. **A BOM/CPL export milestone, then the fab round.** Queued immediately after
   M19a/M19b. The board gets ordered.
3. **M19c, and any placement implementation from [M20](m20-placement-quality.md),
   after the board is ordered.** Both can move copper on `pcie-sata`, and neither is
   worth moving it underneath a board that is in flight.

M19d is a measurement and runs whenever the work it measures has landed.

## What is explicitly *not* in this milestone

* **Placement.** It is [M20](m20-placement-quality.md), evaluated now and scheduled
  after the fab round. The short version of why it is next: M17 proved the via
  opportunity ten times smaller than M16's proxy suggested and M17c halved the
  runtime, so the remaining quality gains most likely live *upstream* of the router.
* **C3, the detour pass — rejected**, not deferred. ~3.4 % expected copper for a
  +30 % runtime budget fails any budget this project has. The numbers are recorded
  in the [postmortem note](../notes/toporouter-postmortem.md#c3-a-detour-pass--rejected-at-m19)
  §C under the closure rule, which is where a rejected candidate goes so that it is
  met by the next person who proposes it.
* **Nothing from §2 of the survey** (virtual capacity, the Gompertz anneal, rip-up
  ordering) is *implemented* here. M19s builds the board they are blocked on, which
  is the prerequisite; evaluating them is M20's or a later milestone's.
* **Nothing from §3.** Learned and GPU routing are rejected with reasons in the
  survey; §3.5 lists what would reopen the question.
* **No parallelism.** If it is ever proposed, §3.6 says the bar is *serial
  equivalency*, not determinism, because the golden files pin the serial answer.
* **No arcs.** The postmortem's argument stands: if arc output is ever built it is a
  separate post-pass over finished polylines, not part of the tightener.
* **No multi-sheet schematic output**, despite M19s producing the design that
  unblocks it. That is a separate milestone against a separate subsystem.

## Acceptance

* **M19s**: an example under `examples/` with more nets than channels on at least
  one cut, shown from `check_capacity`'s report; it routes or hands over cleanly and
  the report says which; iterations-to-convergence recorded; its `router_seconds`
  published against M17d's prediction for its connection count; `baseline.json`
  refreshed in the same commit with before/after corpus totals; byte-stable.
* **M19a**: ≥ 10 % corpus `router_seconds`, **all board hashes byte-identical**, the
  tie-break tested explicitly. Hash preservation is the criterion, not a hope.
* **M19b**: ≥ 25 % further, completion unchanged, copper unchanged; any hash that
  moves is explained per board and per connection.
* **M19c** (if the gate opens): ≥ 30 % further, byte-identical hashes, its own ADR,
  E3 discharged first, **hypothesis property tests against shapely including the
  degenerate classes**, and **a kill-switch config with the rebuild path retained
  and the predicate opt-in** until the full corpus and M19s have run through it.
* **M19d**: new exponents published against M16's and M17's, over eleven boards for
  comparability and over twelve for the better fit, with the method restated.
* **Every change**: `bench --compare` against `bench/results/baseline.json` in the
  delivery report, corpus **and per board**; deterministic;
  `test_routing_is_byte_stable` and `test_stretching_is_deterministic` green; `ruff`
  and `mypy --strict` clean; the baseline refreshed in the same commit as the change
  that moves it, with the report saying why the new numbers are better.

## Guardrails

* The committed baseline is the judge; nothing lands without its compare.
* No timer goes into `src/`. Profiling is driven from scratch scripts outside the
  repository, as M17 and M18 both did.
* Determinism is not part of the trade and is not negotiable.
* Licence wall: implement from the note and the cited papers, never from source.

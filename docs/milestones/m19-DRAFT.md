# DRAFT — Router improvements, part 3: the field that gets rebuilt (M19)

> **THIS IS A DRAFT AND NOT AN APPROVED MILESTONE.** It was written by the M18
> survey session as its second deliverable, for the owner's review. Nothing here is
> scheduled, and no implementation has begun. The decisions the owner has to make
> before it becomes a milestone are listed at the bottom under
> [Decisions required](#decisions-required-before-this-becomes-a-milestone).

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
constructs the same thing twice, in increasing order of cost and risk.

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

**Budget: ≥ 10 % reduction in corpus `router_seconds`, and every one of the eleven
board hashes byte-identical.** A performance change that moves a coordinate is a
quality change wearing a disguise (M17c §3.3); this one has no excuse to move
anything, because it computes the same predicate.

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
completion (275/278) and unchanged copper.** Board hashes are *expected* to hold
but are not guaranteed — a bounded field can find a different-but-legal corridor. If
one moves, the report explains which board, which connection, and by how much, or
the candidate is rejected.

## M19c — An incremental free-space mesh (survey candidate L1) — **conditional**

**Do not start this if M19a and M19b together land more than half of the corpus
routing time.** The survey's whole ordering argument is that the cheap candidates
take a bigger bite than this one, and if they do, L1's remaining prize is much
smaller than the profile makes it look today.

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
connections attempted, and against connections × cuts, over all eleven examples —
and publish the new numbers alongside M16's (1.72 / 1.12) and M17's (1.70 / 1.10).

**The interesting question is whether the exponents move this time.** M17c removed
constant factors and they did not. M19b changes the *shape* of the repair cost from
whole-board to bounded, and M19c changes it from rebuild-per-connection to
update-per-connection. If those exponents still do not move, that is a finding worth
as much as the runtime.

## Rejection rule

Unchanged from part 2, and it applies to every candidate above: **a candidate that
misses its budget or regresses quality is rejected, with its numbers written into
[`docs/notes/routing-literature.md`](../notes/routing-literature.md) §5** — the note
whose §4 proposed it — rather than deleted or left in a branch. A negative result
nobody writes down gets proposed again.

M19c carries one extra rejection clause of its own: **if the ADR cannot be written
honestly, the candidate is rejected without being built.** "We think the predicates
will be fine" is not an ADR.

## What is explicitly *not* in this milestone

* **Nothing from §2 of the survey** (virtual capacity, the Gompertz anneal, rip-up
  ordering). All of it is gated on a board that does not converge in one iteration,
  and no such board exists in the corpus. **That board is a prerequisite, and it is
  already one of the [graduation conditions](../roadmap.md#maturity-and-graduation).**
* **Nothing from §3.** Learned and GPU routing are rejected with reasons in the
  survey; §3.5 lists what would reopen the question.
* **No parallelism.** If it is ever proposed, §3.6 says the bar is *serial
  equivalency*, not determinism, because the golden files pin the serial answer.
* **No arcs.** The postmortem's argument stands: if arc output is ever built it is a
  separate post-pass over finished polylines, not part of the tightener.
* **C3, the detour pass**, is not here either — but its expected gain has been
  re-priced by the survey from 7–16 % down to **~3.4 %** on Dayan's own primary
  measurement, and the roadmap entry says so now.

## Acceptance

* M19a: ≥ 10 % corpus `router_seconds`, **all eleven hashes byte-identical**, the
  tie-break tested explicitly.
* M19b: ≥ 25 % further, completion 275/278 unchanged, copper unchanged; any hash
  that moves is explained per board and per connection.
* M19c (if run): ≥ 30 % further, byte-identical hashes, its own ADR, E3 discharged
  first.
* M19d: new exponents published against M16's and M17's, with the method restated.
* Every change: `bench --compare` against `bench/results/baseline.json` in the
  delivery report; deterministic; `test_routing_is_byte_stable` and
  `test_stretching_is_deterministic` green; `ruff` and `mypy --strict` clean; the
  baseline refreshed in the same commit as the change that moves it, with the report
  saying why the new numbers are better.

## Guardrails

* The committed baseline is the judge; nothing lands without its compare.
* No timer goes into `src/`. Profiling is driven from scratch scripts outside the
  repository, as M17 and M18 both did.
* Determinism is not part of the trade and is not negotiable.
* Licence wall: implement from the note and the cited papers, never from source.

## Decisions required before this becomes a milestone

The survey session stopped here by design. These need the owner:

1. **Is M19c in or out?** It is the only item in this project's history that would
   partially reverse an ADR and put a hand-owned geometric predicate into the
   router — the exact failure mode that killed the gEDA toporouter. The survey
   recommends **gating it on M19a and M19b's measured results** rather than deciding
   now, but the shape of the milestone differs a lot depending on whether it is
   scoped in from the start.
2. **Does the congestion stress board come before or after this?** Every §2
   candidate (L3, L4) is blocked on it, it is a graduation condition, and it is a
   different kind of work from anything above. If it comes first, M19 shrinks to
   M19a/M19b and the §2 candidates become M20.
3. **Does the `pcie-sata` fab round still gate part-2-style work?** The roadmap says
   part 2 comes after fabrication *by decision rather than by dependency*, so that
   the copper measured and the copper produced are the same copper. **M19a is
   hash-preserving by its acceptance criterion and is arguably exempt**; M19b and
   M19c are not. The owner should say which.
4. **Should the survey's re-pricing of C3 change its place in the roadmap?** Dayan's
   own measurement is ~3.4 % wire length for a rip-up-and-reroute optimiser, against
   the 7–16 % the roadmap currently cites from Blake, and M17 found 53 of 55
   collapsible spans on this corpus unimprovable. C3's budget is +30 % runtime.
   Those numbers no longer look like a good trade, and the roadmap entry has been
   updated to say so — but whether C3 stays a candidate at all is the owner's call.
5. **Is a second attempt worth making on the three closed sources?** Lu's 1991
   dynamic-CDT thesis (UCSC), NCTU-GR's two-stage cost function (IEEE), and any
   algorithmic account of TopoR (the 2005 LETI textbook is the likeliest place).
   All three are recorded as gaps. Only the first would change M19c's design, and
   only marginally — Shewchuk & Brown and Livesu et al. supersede it.

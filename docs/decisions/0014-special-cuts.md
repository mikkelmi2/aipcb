# 0014 — The cuts the triangulation never drew, and what the capacity check may claim

* **Status:** Accepted
* **Date:** 2026-08-23
* **Context:** milestone M16a, acting on the
  [gEDA toporouter postmortem](../notes/toporouter-postmortem.md) §A.6 and its
  flagged exposure E1. Amends the congestion model recorded in
  [ADR 0006](0006-routing-approach.md) and the cut-capacity design in
  [`field.py`](../../src/aipcb/route/field.py).

## Context

[`field.py`](../../src/aipcb/route/field.py) states Maley's realizability
criterion — a set of topologies can be turned into legal geometry exactly when no
*cut* across the free space is over-subscribed — and then implements it as *every
interior edge of the triangulation is a cut*. Those are not the same statement.
Maley quantifies over all cuts; a triangulation offers the ones it happened to
draw.

The postmortem found the gap by reading the only shipped implementation of this
idea that exists. Take two triangles sharing a diagonal `e`:

```
        opv                                opv
        / \                                /|\
       /   \                              / | \
    e1/     \e2                          /  |  \
     /   t   \                          /   |   \
    /         \                        /    |    \
  v1-----e-----v2                    v1     |    v2
    \         /                        \    |   /
     \  opt  /                          \   |  /
      \     /                            \  | /
       \   /                              \ |/
        opv2                               opv2
```

A wire that enters `t` through `e1` and leaves through `e2` never crosses `e` at
all — it rounds the apex instead. It does cross the *other* diagonal of the
quadrilateral, `opv`–`opv2`, and that segment is the room it actually has. The
constrained Delaunay triangulation declined to draw it, which is precisely the
case where it is the shorter one. Anthony Blake hit this in gEDA PCB's toporouter
in 2009, named them **special cuts**, and rewrote his congestion accounting around
them.

So `check_capacity` was optimistic: it could pass a board Maley's criterion
rejects. The consequence is bounded — the stretcher is constructive and
[`invariant.py`](../../src/aipcb/route/invariant.py) asks the finished board
whether two nets overlap, so what ships is a route that fails or detours absurdly,
never a short circuit — but the check claimed more than it delivered, silently,
which is the part that had to stop.

**Source discipline.** gEDA PCB is GPL-2.0 and aipcb is Apache-2.0. Everything
below is derived from the postmortem note's own description and from Maley's
criterion, never from the GPL source. The identifiers in the note appear there as
addresses so a reader can find what is being described; nothing was transcribed.

## What was measured

On 2026-08-23, Shapely 2.1.2 / GEOS 3.13.1, CPython 3.14.4 on Linux, against every bundled example, before any
implementation was chosen. Candidate C2 asked for exactly this: *count how often a
flip diagonal is shorter than the diagonal it pairs with, and if the answer is near
zero on real boards, scope this as a correctness tidy-up rather than a quality
win.*

**Finding 1 — it is not near zero.** Per layer, the share of CDT diagonals whose
second diagonal exists (the quadrilateral is convex) and is *shorter*:

| Example | Layer | Diagonals | Convex pairs | Flip shorter | Share | Shortest ratio |
|---|---|---:|---:|---:|---:|---:|
| congestion | B.Cu | 161 | 109 | 24 | 14.9% | 0.440 |
| congestion | F.Cu | 161 | 109 | 24 | 14.9% | 0.440 |
| diff-pair | B.Cu | 179 | 128 | 31 | 17.3% | 0.040 |
| diff-pair | F.Cu | 269 | 189 | 44 | 16.4% | 0.057 |
| enclosure | B.Cu | 207 | 167 | 59 | 28.5% | 0.053 |
| enclosure | F.Cu | 487 | 360 | 98 | 20.1% | 0.055 |
| ldo-supply | B.Cu | 65 | 38 | 16 | 24.6% | 0.035 |
| ldo-supply | F.Cu | 245 | 171 | 30 | 12.2% | 0.035 |
| led-blinker | B.Cu | 259 | 200 | 46 | 17.8% | 0.026 |
| led-blinker | F.Cu | 379 | 287 | 64 | 16.9% | 0.044 |
| mcu-4layer | B.Cu | 341 | 266 | 56 | 16.4% | 0.026 |
| mcu-4layer | F.Cu | 607 | 453 | 98 | 16.1% | 0.040 |
| mcu-4layer | In1.Cu | 341 | 266 | 56 | 16.4% | 0.026 |
| mcu-4layer | In2.Cu | 341 | 266 | 56 | 16.4% | 0.026 |
| overconstrained | B.Cu | 153 | 95 | 23 | 15.0% | 0.475 |
| overconstrained | F.Cu | 153 | 95 | 23 | 15.0% | 0.475 |
| pcie-sata | B.Cu | 191 | 92 | 21 | 11.0% | 0.235 |
| pcie-sata | F.Cu | 1399 | 810 | 190 | 13.6% | 0.236 |
| pcie-sata | In1.Cu | 24 | 13 | 1 | 4.2% | 0.235 |
| pcie-sata | In2.Cu | 24 | 13 | 1 | 4.2% | 0.235 |
| qfn-fanout | B.Cu | 183 | 142 | 31 | 16.9% | 0.084 |
| qfn-fanout | F.Cu | 660 | 419 | 108 | 16.4% | 0.097 |
| routing-demo | B.Cu | 251 | 199 | 43 | 17.1% | 0.038 |
| routing-demo | F.Cu | 341 | 267 | 53 | 15.5% | 0.038 |
| usb-port | B.Cu | 157 | 109 | 31 | 19.7% | 0.028 |
| usb-port | F.Cu | 333 | 233 | 41 | 12.3% | 0.028 |

Every layer of every bundled example, nothing selected. Between **4% and 29%** of
diagonals per layer have a shorter partner, and the shortest measured anywhere is
**2.6%** of the diagonal it pairs with (`led-blinker` and `mcu-4layer`, B.Cu) — a
corridor the old model priced at its diagonal's full length that is really a
fortieth of it. The one low outlier, `pcie-sata`'s inner layers at 4.2%, is a plane
with 24 diagonals on it: almost nothing is triangulated there to begin with.

This is ordinary board geometry, not a corner case, so C2's fallback scoping --
"a correctness tidy-up rather than a quality win" -- does not apply.

**Finding 2 — charging them finds pressure the old cut set could not see.** Every
example was routed and every finished leg charged to both cut families:

| Example | CDT cuts | over | Flip cuts | over |
|---|---:|---:|---:|---:|
| congestion | 322 | 0 | 218 | 0 |
| diff-pair | 448 | 1 | 317 | **2** |
| ldo-supply | 310 | 3 | 209 | 3 |
| mcu-4layer | 1630 | 2 | 1251 | 1 |
| pcie-sata | 1638 | 25 | 928 | 18 |
| qfn-fanout | 843 | 1 | 561 | 0 |
| routing-demo | 592 | 0 | 466 | **1** |
| usb-port | 490 | 1 | 342 | 0 |

`routing-demo` is the clean demonstration: nought over-subscribed diagonals, one
over-subscribed flip diagonal. The old accounting called that board's corridors
uniformly comfortable and there is a pinch in it.

**Finding 3 — the crossing predicate does not matter, so the conservative one
wins.** A tightened route hugs an inflated obstacle corner by passing *through* the
vertex, so a wire rounding an apex meets the cut hinged on that apex exactly at its
endpoint. Under Shapely's strict `crosses` — interiors must meet — that wire rounds
the apex for free, which is the whole failure being closed. Under `intersects`,
which is what `cuts_crossed` already used for CDT diagonals, it is charged. Both
predicates were run over the whole corpus and **flagged identical sets of
over-subscribed cuts on every example**, so the choice costs nothing and the safe
direction is free.

**Finding 4 — the cost is under 1%.** Deriving the cuts is one pass over the
diagonals with four orientation tests each, and charging them is one more R-tree
query per leg. Best of five runs, same process, same board:

| Measurement | Without | With | Delta |
|---|---:|---:|---:|
| `check_routes` on `routing-demo` | 638.7 ms | 643.5 ms | **+0.8%** |
| `check_routes` on `usb-port` | 395.1 ms | 398.3 ms | **+0.8%** |
| deriving them, `led-blinker` F.Cu (357 diagonals) | — | 0.52 ms | — |
| deriving them, `pcie-sata` F.Cu (866 diagonals) | — | 1.11 ms | — |

Those two examples are the measurement, because they are the only bundled designs
that declare `layout.routes` — `check_capacity` returns immediately on a design
with no sketches, so the other nine would have measured nothing. The derivation
scales as the diagonal count, and doubling the board roughly doubled it.

## Decision 1 — charge the second diagonals, in the check

The second diagonal of every **convex** adjacent triangle pair joins the cut set,
with capacity equal to its own length plus one clearance — the same convention a
CDT diagonal uses, and correct for the same reason: the free space is already
inflated by a clearance, so a gate of length *L* holds *n* tracks exactly when *n*
widths and *n*−1 inter-track clearances fit.

Convexity is the soundness condition and it is checked rather than assumed. A CDT
is free to leave a pair unflipped precisely because flipping it would put the new
diagonal *outside* the free space, and a segment that leaves the free area is not a
cut across it. `special_cuts()` requires the two apexes to fall on opposite sides
of the shared diagonal *and* the shared diagonal's ends to fall on opposite sides of
the apex-to-apex line.

## Decision 2 — the router is not charged for them yet, and the reason is a number

`build_field(..., special_cuts=True)` is opt-in, and only
[`check_capacity`](../../src/aipcb/route/check.py) and
[`aipcb bench`](../../src/aipcb/bench.py) opt in. Negotiation still costs paths
against the diagonals alone.

This is deliberate and it is the milestone's own guardrail. Charging the new cuts
inside the cost model changes which corridor the router picks, which moves every
congestion-sensitive golden file and trades runtime for quality — and M16's whole
thesis, taken from the toporouter's autopsy, is that a trade of that shape must be
measured before it is made. The benchmark that would measure it was built in the
same milestone (M16c) and did not exist when this was written. It is a roadmap
part-2 item with a stated budget, not an omission.

## Decision 3 — the check states its limit, in the words a user reads

Even with the second diagonals, the cut set is a **subset**: any segment between two
obstacle vertices that spans more than one triangle pair is a cut nothing here
charges. So the wording is corrected in all three places the claim appears —
`field.py`'s module docstring, `check_capacity`'s docstring, and
[`docs/topology.md`](../topology.md) — to say that this is a **lower bound on
congestion, not the criterion in full**. A clean result is evidence, not a proof.

Separately, the diagnostic now names *which kind* of cut it found, because "a
corridor on F.Cu" and "the gap on F.Cu they have to round a corner through" send a
reader to different places on the board.

This half of exposure E1 was always separable from Decision 1 and would have been
done even if the cuts had never been charged. A check that overstates its guarantee
is worse than one that states its limits, because the first kind is trusted.

## Consequences

* `check_capacity` reports two kinds of over-subscription and names which. The
  diagnostic code is unchanged (`route-cut-over-subscribed`); the `over_subscribed`
  entries in the JSON gain a `"cut"` field of `"diagonal"` or `"special"`.
* No golden file moved, because the router's cost model did not.
* `Triangulation.special_cuts()` is derived once and cached, so a field that asks
  for them pays for them once per layer.
* The gap that remains is named rather than closed. If a board is ever found that
  `check_capacity` passes and no router can build, the next cut family to charge is
  segments spanning more than two triangles — and this ADR is where that
  conversation starts.
* **Re-measure at each Shapely major**, per [`CLAUDE.md`](../../CLAUDE.md): the
  convexity test and the crossing predicate both rest on GEOS behaviour, and
  Finding 3 in particular is an empirical claim about two Shapely predicates
  agreeing.

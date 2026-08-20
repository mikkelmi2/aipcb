# 0006 — Topological routing: stored sketches, derived homotopy, funnel tightening

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M7 (M7a–M7d)

## Context

M7 is the flagship. Routes are to live in the source as *topology* — which
obstacles a route passes and on which side — and a deterministic "stretcher" turns
that topology into DRC-clean geometry. The payoff is incremental: when placement
moves, the topology is still valid and only the tightening re-runs.

The brief names three bodies of prior work to read before designing anything.
Having read them, this ADR records what was taken from each and what was decided.

## What the prior work says

**Dai, Kong, Leong, Dayan and Staepelaere — SURF** (DAC 1991 and after)
introduced the *rubber-band sketch*: interconnect represented as rubber bands
rather than as fixed geometry, with a grid-less, any-angle wiring model. SURF is
the origin of the idea this milestone is built on — that the durable description of
a route is its topology, and geometry is a rendering of it. Its multi-layer
topological router routes all nets simultaneously by hierarchical top-down
partitioning with successive refinement.

**Maley — *Single-Layer Wire Routing and Compaction*** (MIT Press, 1990) supplies
the theory: wires as flexible connections with fixed topology, routings
characterised by simple topological invariants, and homotopy as the equivalence
relation that says two routings are "the same route". This is what makes
"the topology is still valid after placement moves" a precise statement rather than
a hopeful one.

**TopoR** (Eremex) is the commercial demonstration that the approach works on real
boards: no preferred routing directions, free-angle traces, abstracting the
geometric design space into a topological graph and letting each net find its own
path in it. It is also evidence for a practical detail — that any-angle output is
acceptable to fabs and to downstream tooling.

The concrete algorithm comes from the shortest-homotopic-path literature
(Tompa 1981; Chazelle 1982; Lee & Preparata 1984; Leiserson & Maley 1985), as
presented in Erickson's computational-topology notes:

1. **Triangulate** the routing area — the board polygon minus the obstacle
   polygons — in `O(n log n)`.
2. Represent a path's homotopy class by its **crossing sequence**: the ordered list
   of triangulation diagonals it crosses.
3. **Reduce** that sequence by cancelling adjacent equal symbols. The reduced
   sequence is a canonical form for the homotopy class.
4. The reduced sequence defines a **sleeve** — a strip of triangles that is
   topologically a disk even when it revisits triangles geometrically.
5. Walk the sleeve with the **funnel algorithm**, maintaining an apex and two
   concave chains, to produce the shortest path in that homotopy class.

Total: `O(n log n + nk)`. Crucially, the algorithm needs *no modification* for
polygons with holes — the sleeve is a topological disk regardless, because the
algorithm only ever makes local decisions (orientation tests) and self-intersection
is a global property.

## Decision 1 — the source stores a sketch, not a crossing sequence

A crossing sequence is defined against a *particular* triangulation. Move one
component and the triangulation changes, so a stored crossing sequence becomes
meaningless. Storing one would destroy the very property M7 exists for.

So the source stores what the brief describes: **an ordered list of the obstacles a
route passes and the side it passes on**, plus via nodes where it changes layer.
Obstacles are named by things that survive placement — a pad (`U1.7`), a via, a
component body — never by coordinates.

The crossing sequence is *derived*, per build, from the current placement:

```
source sketch  ──►  reference polyline  ──►  crossing sequence  ──►  sleeve  ──►  funnel  ──►  tracks
   (durable)          (placement-dependent, all recomputed every run)
```

This is what makes re-tightening after a placement change cheap and correct: the
durable half is untouched, and only the derived half re-runs.

## Decision 2 — sides are recorded relative to travel direction

A waypoint says the route passes an obstacle on its **left** or **right**, oriented
along the direction of travel from the route's start to its end. The alternative —
absolute compass sides — is not invariant under the board being rotated or
mirrored, and reads wrongly the moment a route doubles back.

## Decision 3 — obstacles are convex hulls inflated by clearance

Each obstacle becomes a convex polygon: the pad or body outline, inflated by
`clearance + track_width / 2` for the net being routed. Tightening against inflated
hulls means a geometrically shortest path is automatically a legal one — the
clearance rule is satisfied by construction rather than checked afterwards and
patched. Convexity keeps the funnel's orientation tests simple and is what the
rubber-band model assumes.

## Decision 4 — any-angle segments, not 45-degree

Rubber-band tightening naturally produces any-angle geometry, and that is what
SURF and TopoR emit. Forcing the result onto a 45° grid would mean a second
correction pass that can reintroduce clearance violations — solving the problem
twice and getting a worse answer. KiCad, Gerber and every fab accept arbitrary
angles. Arcs at hull corners are a later refinement, not a correctness matter.

## Decision 5 — the stretcher is a pure function

`(topology, placement, rules) -> tracks`, with no state and no I/O. That is what
makes it testable, trivially deterministic, and safe to re-run on every build.
It uses `shapely` for polygon operations and `scipy`'s constrained Delaunay
triangulation, per the brief's guidance to prefer well-understood computational
geometry over invented algorithms.

## Staging

| Stage | Scope |
|---|---|
| **M7a** | The topology model and its validation. `aipcb route check` verifies a sketch is realizable against the current placement. Documented in `docs/topology.md`. |
| **M7b** | The stretcher: obstacle hulls, triangulation, crossing sequence, funnel, KiCad tracks. Single layer plus via hops. Acceptance: DRC-clean, byte-stable. |
| **M7c** | Auto-topology: derive a sketch for unrouted nets by searching the triangulation dual with cost = length + congestion, then tighten. |
| **M7d** | Differential pairs (coupled tightening, gap from impedance), skew and length matching by meander insertion, layer preferences. |

The acceptance bar is identical at every stage: generated tracks pass
`kicad-cli pcb drc` on the example boards, and are byte-stable across runs.

## Sources

* Dai, Dayan, Staepelaere, "Topological routing in SURF: generating a rubber-band
  sketch", DAC 1991 — <https://dl.acm.org/doi/pdf/10.1145/127601.127622>
* Dai, Kong, Sato, "Routability of a rubber-band sketch" —
  <https://www.semanticscholar.org/paper/Routability-of-a-rubber-band-sketch-Dai-Kong/103a2d8b33ce3de0710f3ab11de7f2f9c25947fd>
* Maley, *Single-Layer Wire Routing and Compaction*, MIT Press —
  <https://mitpress.mit.edu/9780262132503/single-layer-wire-routing-and-compaction/>
* Erickson, "Shortest (Homotopic) Paths", computational topology notes —
  <https://jeffe.cs.illinois.edu/teaching/compgeom/notes/05-shortest-homotopic.pdf>
* TopoR — <https://en.wikipedia.org/wiki/TopoR>

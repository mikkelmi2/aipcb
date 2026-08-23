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

---

## Amendment (M14d/M14e): declared-manual routing, and a bridge that is not an integration

* **Status:** Accepted
* **Date:** 2026-08-22
* **Context:** milestone M14d, M14e

### Integration is still rejected; the position is refined

Nothing above changes. aipcb does not embed, call, or select an external router, and
this amendment does not move towards doing so. What it adds is the sanctioned path
for people who want one anyway — because "we do not integrate an external router"
was, until now, silently also "and we will not help you use one", which was never
the intent and which an agent could not act on.

The refinement is one sentence: **aipcb provides the pipes on each side and judges
the result; the orchestration is the agent's, visibly.**

### `routing: manual` — intent, not accident

Manual routing already *happened*: the router hands a connection over when it cannot
deliver legal geometry (M9f), and M6 preserves whatever a human draws. What did not
exist was a way to say so in advance. "This pair is mine, do not route it" was a fact
about a workflow rather than a fact about the design — undiffable, unvalidatable, and
impossible for an agent to act on without being told out of band.

`routing: manual` on a net class covers every net in it; on a net it overrides the
class in either direction. The router excludes those nets once, before anything is
paired, negotiated or sketched — a net skipped in one place and routed in another
would be worse than not having the field at all.

Reports now distinguish four states rather than collapsing them into routed and
unrouted:

| State | Meaning |
|---|---|
| `manual-routed` | declared manual, and copper for it is on the board |
| `manual-pending` | declared manual, and there is no copper yet |
| `auto-routed` | aipcb's router laid it |
| `handed-over` | aipcb's router tried and refused, with the reason |

`manual-pending` is the one that earns the field. It is where a board sits between
"these pairs are mine" and "I have drawn them", and to anything counting unrouted
connections it looks exactly like a finished board.

**A pattern generator is not the router.** A net that is declared manual *and* lands
on a package with a `fanout:` block still gets its escape pattern, and the same goes
for a declared pair `transitions:` entry. Both declarations are explicit, and
suppressing the pattern would silently disable the one the user wrote a whole block
for — the worse of the two surprises. Neither is allowed to be a surprise, so the
overlap is reported as `manual-net-has-generated-pattern`. Measured on
`examples/qfn-fanout` with `IO_PD0` declared manual: the negotiated router leaves it
alone and the fan-out escape is still laid.

### Phase 0: what was measured, and when

Per the standing rule that an ADR's premises about external tools have expiry dates,
this was measured on **KiCad 9.0.8** and **Freerouting 2.3.0** on **2026-08-22**,
before any bridge code was written.

| Question | Finding |
|---|---|
| `kicad-cli` DSN export / SES import | **Absent.** `kicad-cli pcb` offers `drc`, `export`, `render`; `export` has no Specctra format. The CLI has not caught up and could not be assumed to have. |
| `pcbnew` DSN export | **Present and headless.** `ExportSpecctraDSN` wrote a 47 508-byte DSN for `examples/pcie-sata` with `DISPLAY` unset. |
| `pcbnew` SES import | **Present and headless.** `ImportSpecctraSES` imported a hand-written session into an unrouted `examples/led-blinker` and saved the board. |
| Freerouting headless CLI | **Works.** `freerouting -de in.dsn -do out.ses` completed with no display, warning `Couldn't get screen resolution` and proceeding; exit 0. |

So the bridge runs through the `pcbnew` subprocess boundary [ADR 0009](0009-pours.md)
already drew for the zone filler: the same rule (no `pcbnew` in aipcb's own process),
the same version lock against `kicad-cli`, the same standalone script that imports
nothing from aipcb.

### Two ways to lose copper, and two mechanisms

Both were found by measurement, and the second was found the hard way.

**On the way out**, KiCad exports existing copper into the DSN's `wiring` section as
`(type route)`, which tells the external router it may rip all of it up. Every wire
and via is rewritten to `(type fix)` before the file is handed over — in the `wiring`
section only, because the same token appears in `structure` rules meaning something
else and a global replace would corrupt the file.

**On the way back**, `pcbnew.ImportSpecctraSES` does not *add* a session's routing to
a board. It **replaces** the board's routing with it. Measured: importing a session
that routed four ISP signals into `examples/mcu-4layer` removed 97 tracks and 52
stitching vias. Nothing said so — the file parsed, the import returned success, and
DRC found no errors, because copper that is gone violates no rule. So the session is
imported into a scratch copy, and only the copper for nets that were actually pending
is spliced into the real board; net codes are matched by *name*, because the importer
renumbers. Copper the session carried for a fixed net is counted, ignored, and
reported.

### Verified, not trusted

SES import reconstructs net classes from names. Every track width and via size that
comes back is compared against the class the source declares, and any difference is
reported as a finding. It is never corrected: silently widening somebody else's track
would make this bridge a router.

### The rule with teeth

A controlled-impedance class carries a width derived from the stackup, a gap that is
an input to that derivation, a coupling budget, a maximum skew and a named reference
plane (M11). An external router knows about none of them, and half of M11's
verification is about *how* a pair was built rather than where it ended up — so aipcb
cannot check what it did not decide. Exporting a declared-manual pending net on such
a class raises `controlled-impedance-to-external-router`.

A warning rather than a refusal: somebody may have a reason, and this program does
not know every board. But never quietly.

### Explicitly not built

No `--engine` flag, no invocation of any router from aipcb, no parsing of its logs,
and no promise about its output beyond `aipcb check`'s verdict on the copper. External
copper is *manual* copper: preserved per element, no source mapping, and **exempt from
the determinism bar** — two Freerouting runs on one DSN differ, that is expected, and
the SES file rather than the run is the reproducible artefact. `docs/external-routers.md`
says all of this to the user rather than leaving it to be inferred.

# 0007 — True multilayer routing: layered triangulations, via columns, negotiated congestion

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M8 (M8a–M8d), building on [ADR 0006](0006-routing-approach.md)

## Context

M7 delivered topological routing on one layer. Via hops are in the source model and
are validated, but the stretcher refuses them, so every route is single-layer. Two
of the bundled examples cannot be routed at all as a result, and not because the
router is weak: `usb-port`'s Micro-B receptacle has a 0.65 mm pad pitch, and at
0.25 mm tracks with 0.2 mm clearance a track needs 0.65 mm of corridor between two
pad edges. There is no room. `led-blinker`'s DIP-8 has the same problem in the
middle of the part. Escaping a fine-pitch part is what the second layer is *for*;
no amount of cleverness on one layer substitutes for it.

M8 makes layer choice, via placement and congestion one optimisation rather than
three afterthoughts. It is an evolution of M7's machinery: the topology model, the
funnel stretcher and the DRC loop all survive.

## What the prior work says

**McMurchie & Ebeling — PathFinder** (FPGA 1995, "A Negotiation-Based
Performance-Driven Router for FPGAs"). Nets are routed one at a time by directed
search over a shared resource graph, and are allowed to *share* resources
illegally. After each iteration the cost of an over-used resource rises, and every
net is ripped up and re-routed against the new costs. The cost of a resource is

```
cost(n) = (b(n) + h(n)) * p(n)
```

where `b` is the base cost, `h` is history — accumulated over iterations, resolving
second-order congestion — and `p` is the present sharing penalty, which resolves
first-order congestion within an iteration. The key insight is that a net only
gives up a resource when the resource is genuinely contested, and that the amount
of contention, not the routing order, decides who gets it. That directly answers
the biggest weakness of M7c: order sensitivity.

**Dai, Kong, Sato and Staepelaere — SURF** (DAC 1991, and "Routability of a
rubber-band sketch"). A rubber-band sketch is realisable iff no *cut* is
over-subscribed: for every cut across the free space, the total width of the wires
crossing it, plus the clearances between them, must fit in the cut's length. This
is a purely local criterion, checkable per triangulation edge, and it is what makes
"is this set of topologies legal" a question that can be answered before any
geometry exists. SURF also establishes the multi-layer topological model this
milestone adopts: one sketch per layer, joined at vias.

**Maley — *Single-Layer Wire Routing and Compaction*** (MIT Press, 1990). The
theory behind the cut criterion: a topological routing with fixed homotopy can be
compacted into legal geometry exactly when every cut has room. Maley's result is
why the router can decide congestion symbolically and trust that the stretcher will
find geometry for it.

Taken together they give the shape of M8: **a shared, layered resource graph whose
resources are triangulation edges with cut capacities, searched per net, negotiated
across iterations.**

## The M7a data-model assumptions that are single-layer, and what happens to them

The brief asks for these to be listed before anything is touched. They divide into
three groups.

### Already multilayer, kept as is

| Assumption | Why it survives |
|---|---|
| `RouteTopology.layer` + `ViaHop.to_layer` | The *source* format was designed multilayer in M7a precisely so this milestone would not have to change it. It stays byte-for-byte compatible. |
| `Obstacle.layers: frozenset[str]` | Already per-layer, already understands `*.Cu`. Through-hole pads block every layer, SMD pads block one; both already work. |
| `RoutingEnvironment.blocking(net, layer, …)` | Already takes a layer. It is called once per layer instead of once. |
| The funnel (`funnel.py`) | Pure planar geometry. A via is a fixed endpoint of a leg, so tightening does not change at all. |

### Single-layer, and changed

| Assumption | Problem | Change |
|---|---|---|
| `StretchResult` has one `layer` and one polyline | A connection that changes layer is not representable | A connection becomes a `RoutedConnection`: an ordered list of per-layer legs plus the `Via`s joining them. `StretchResult` stays the per-leg type. |
| `RoutedBoard.routed`/`.endpoints` pair one result to one pad pair, positionally | One connection is now several results | Endpoints move *into* `StretchResult` (`start`, `end`), and the positional list goes. A leg's endpoint may be a via (`via:n7`), not only a pad. |
| `route_board(layer="F.Cu")` — a single global routing layer | Layer is a search variable now, not a parameter | Replaced by a `LayerStack` derived from `layout.stackup`. The CLI flag becomes `--layers` and defaults to the stackup's signal layers. |
| `Triangulation` has no layer identity | Congestion must be attributed per layer | One triangulation per signal layer, in a `LayeredField`, keyed by layer name. |
| Congestion is implicit in geometry: laid tracks shrink later gates | **Rip-up is impossible.** Removing a route means rebuilding every triangulation from scratch, and there is no "how full is this corridor" number to negotiate over | Congestion becomes an explicit per-edge quantity: `used_mm`, `capacity_mm`, `history`. Ripping up a route subtracts its demand and nothing else. This is the single most important change in M8. |
| `_track_obstacles` feeds finished geometry back as obstacles | Same problem: order-dependent and irreversible | Kept, but only for the *final* geometry pass. The negotiation runs entirely on the symbolic graph, where rip-up is free. |
| Vias are not obstacles at all | A via is copper on every layer its barrel passes | New `via` obstacle kind, generated on every layer of the barrel's span (M8a's "via column"). |
| `plan._CLASS_ORDER`, an ad-hoc class→int table | Priority is invisible to the source | Replaced by `NetClass.priority`, with the old table becoming the *default* assignment for classes that do not state one. Same numbers, one mechanism. |
| `Stackup` knows only `copper_layers` | No roles, no per-layer thickness, no via types | Adds `planes`, `via_types` and `preferred_direction`. |
| `emit.py` writes only `segment` | No vias in the output | Adds `via` items with derived UUIDs. |
| `check.py` verifies layers exist | No cross-layer capacity check | `route check` gains the cut-capacity check across all layers. |

### Deliberately unchanged

`side: back` placement stays warned-and-ignored: mirroring a footprint is a
placement question, not a routing one, and nothing in the layer work makes it fall
out for free. Copper pours stay out of scope, so a declared plane layer is routing
law rather than generated copper — see "Consequences".

## Decision 1 — one triangulation per signal layer, and a via is a column

Each signal layer gets its own constrained Delaunay triangulation of the board
polygon minus the obstacles *on that layer*: pads that reach it, the board edge,
keepouts naming it, and any manual track preserved from a previous build.

A via is modelled as a **column**: a node that exists as an obstacle on every
copper layer its barrel physically passes through, and that joins the
triangulations of the layers it connects. A through via on a four-layer board is an
obstacle on all four layers even though it carries signal on only two — the barrel
is there regardless, and forgetting that is how a router puts an inner-layer track
through a hole. Blind and buried vias block only their own span, which is the point
of using them, and are only generated when the stackup lists the type.

A route topology therefore becomes: *a sequence of triangulation edges crossed, with
via-column events marking layer transitions.*

## Decision 2 — realizability is the cut criterion, checked per edge

Each triangulation edge has a capacity, and a set of topologies is legal iff no
edge is over-subscribed. Concretely, with obstacles inflated by the reference
clearance before triangulating:

```
capacity(edge)  = length(edge) + reference_clearance
demand(net)     = track_width(net) + clearance(net)
```

The `+ reference_clearance` is not a fudge. Inflating both obstacles by `c` removes
`2c` from the physical gap `D`, so `length(edge) = D − 2c`, while *n* tracks
physically need `n·w + (n+1)·c ≤ D`. Adding one clearance back to the capacity
makes the arithmetic exact rather than pessimistic by one clearance — which
matters, because a gate sized for exactly one track would otherwise be declared
unusable.

This is Maley's criterion and SURF's routability test, and it is what lets the
negotiation work on symbols. `aipcb route check` now reports over-subscribed cuts
across every layer, naming the nets that share them.

## Decision 3 — negotiated congestion, not sequential feedback

M7c routes nets in a fixed order and feeds each finished route back as geometry.
That has two defects: the result depends on the order, and a net that took a
corridor a later net needed cannot be persuaded to move.

M8b replaces it with PathFinder. Every net is routed by A\* over one combined graph
spanning all signal layers; over-used edges get more expensive; over-users are
ripped up and re-routed; repeat. Rip-up is cheap because it removes a *symbolic*
path — a set of edge subscriptions — not geometry.

The cost function, with every term a named parameter documented in
[`routing-costs.md`](../routing-costs.md):

```
cost(edge) = length
           + via_cost × n_vias
           + layer_penalty(layer, net class)     ∞ for plane layers
           + congestion(edge)                    → ∞ as usage approaches capacity
           + direction_penalty                   soft H/V preference from the stackup
```

`congestion` follows PathFinder's shape, `(base + history) × present`, with the
present factor rising on a fixed schedule so early iterations explore and later
ones enforce. The schedule is fixed, not adaptive, so the run is reproducible.

**Priority** drives two things, both from the same source field:

* *Initial ordering* — descending priority, then descending difficulty
  (length × congestion), then route key. Difficulty first-order approximates "how
  little freedom does this net have".
* *Rip-up cost* — `rip_up: protected` multiplies the cost of ripping a net up, so
  low-priority traffic detours around it instead; `never` nets are ripped only as a
  last resort before declaring failure, and the failure report names them.

Unset priorities are filled in from the class name — pairs and matched groups 80,
power 60, everything else 50 — so M7's ordering heuristic becomes the *default
value* of a source field rather than a separate code path.

## Decision 4 — the search picks a via *site*, the stretcher picks its position

Candidate via sites are generated discretely: the incentres of triangles that have
room for a via plus its clearance, plus a coarse grid at roughly twice the via
pitch where triangles are large. Continuous via coordinates are explicitly not
optimised in the search — the brief rules it out and it would make the search
non-combinatorial for no benefit, because the stretcher refines the position
afterwards anyway.

In the stretcher a via is simply a point both layers' rubber bands pull on. Each
leg between two fixed points (pad→via, via→via, via→pad) is tightened exactly as
M7b tightens a whole route.

Two things learned while building it changed the shape of this decision.

**The plan is advice, not instruction.** The search happens on a *shared* field
whose obstacles are inflated by the widest clearance on the board and which knows
nothing about where copper eventually landed; the tightening happens on a per-net
field that knows both. Usually the plan survives contact with the geometry.
Sometimes the corridor it chose has since been taken, and following the plan anyway
means a long detour round copper that was not there when the plan was made. So each
leg is tightened twice — once through the plan's gate midpoints, once directly — and
the shorter is kept, with the plan winning ties. On the bundled examples that is
worth a fifth of the copper.

**A via's position has to be re-checked.** The site was chosen before any copper
existed; by realization time some exists, and a via that no longer clears it is not
a via. Rather than nudging it, the connection is handed to the repair pass below,
which re-searches on a field that knows where the copper actually went.

**The via then settles.** The site came from a discrete set, so the via starts
somewhere reasonable in the right pocket while the two legs meeting there both bend
to reach it — on the four-layer example, 11.5 mm of copper across five vias, one of
them alone worth 5.8 mm. So each via is pulled towards the straight line between its
neighbouring corners, both legs are re-tightened against it, and the move is kept
only if the pair came out shorter *and* both are still legal. That halves the detour
(11.5 mm to 6.2 mm; the rest is vias that genuinely cannot sit on the line). Bounded,
monotone per via, and unable to make a board worse — the worst case is that nothing
moves.

## Decision 4a — a repair pass, because the shared field is an approximation

One field for every net is what makes the congestion figures comparable, and it is
also a lie in two specific ways: obstacles are inflated by the *widest* clearance on
the board rather than each net's own, and the net's own pads block it just as they
block everything else. The second one bites hardest on a connector's shield tabs,
which are all one net and all in each other's way: on the shared field there is no
corridor between two of them at all, while on GND's own field they are two points
with nothing in between.

So a connection the shared field could not place gets one more try on a field built
for that net alone — its own clearance, its own width, its own pads open, and every
piece of copper realized so far included. The free space that produces is exactly
the free space the stretcher will tighten in, so a path found there is realizable by
construction rather than by approximation. Only failures pay for the second field.

The repair runs **immediately**, not at the end. Everything realized after a
connection becomes an obstacle to it, so a repair deferred to the end is a repair
attempted against the most crowded board there will ever be.

## Decision 5 — a pair is one object in the graph, and stays on one layer

A differential pair is routed as a single entity with `demand = 2w + gap + c` and
one path. The two halves are never routed independently and merged, because a merge
cannot restore a property (constant gap) that the independent routing did not have.

Two things about pairs came out differently from the first draft of this ADR, and
both for the same kind of reason: the obvious construction does not survive
arithmetic.

**A paired via column is not two vias at the pair's pitch.** A 0.6 mm via at a
0.54 mm pitch is one piece of copper. For a pair to change layer together the two
halves have to splay out to *via* pitch, transition, and come back — a distinct
pattern with its own impedance discontinuity and its own clearance problem. It is
not built, and a pair is therefore given `max_vias = 0`. A pair that cannot be
routed on one layer says so, which is also the right answer for an
impedance-controlled pair whose reference plane was chosen for the layer it is on.

**A crossover cannot be absorbed by a layer change at all.** Two parallel wires on
one layer form a two-strand braid, and exchanging them requires exactly one
crossing; moving both halves to another layer is a *translation*, not a
transposition. Whichever layer the pair ends up on, the two halves still have to
cross on it. This is planarity, not an implementation gap.

The mechanism that does work is one half diving to the adjacent layer, passing under
the other, and returning — two vias on one net, and a real impedance discontinuity
in one half only. That is not built either. A pair that needs a crossover is
reported, with the observation that swapping the two pads at one end removes it,
which on a board where both ends are your own components is usually the better fix.
Both `usb-port` and `mcu-4layer` are laid out to make it unnecessary, and
`mcu-4layer`'s comments say which choice did it.

## Decision 5a — the fan-out is tightened, and necks down

The coupled part of a pair is legal by construction. The fan-out at each end is
where the two halves leave the pair's own pitch for pads that are somewhere else,
and in M7 it was drawn straight — which is what stopped both of `usb-port`'s pairs
coupling, each refusing with the name of the pad its fan-out would have crossed.

So the fan-out is tightened too, as an ordinary short route between two fixed
points, and it carries the class's ordinary trace width rather than the pair's. That
second part is not a convenience: a 0.34 mm pair trace cannot land on a 0.65 mm-pitch
receptacle pin without breaking clearance to its neighbours, and necking down at the
connector is what a person does, for the same reason. A pair therefore emits three
runs per half — neck, coupled, neck — and each is checked at *its own* width.

## Decision 6 — the stackup states routing law

A layer named under `stackup.planes` is excluded from signal routing by an infinite
layer penalty. A net class opts back in by naming the plane in its `prefer_layers`,
which is the only way a signal reaches a plane layer.

Pouring the plane is out of scope (the brief excludes copper pours), so a plane
layer is a *reservation*: nothing signal-carrying is placed on it, and the nets
that would live on it are still routed as tracks on the signal layers. That is
honest — the board is complete and DRC-clean either way — and it means the plane
declaration does exactly one thing, which is easy to reason about.

## Decision 7 — the source format is extended, never broken

Everything M8 needs is a new optional field with a default that reproduces M7's
behaviour:

```yaml
net_classes:
  clk_sys:
    priority: 90            # 0-100; unset means the per-class default
    rip_up: protected       # never | protected | normal
    layer_forbid: [B.Cu]    # prefer_layers, from M7, is the positive form

layout:
  stackup:
    copper_layers: 4
    planes:
      - layer: In1.Cu
        net: GND
    via_types: [through, blind]
    preferred_direction:
      F.Cu: horizontal
      B.Cu: vertical
```

No existing key changes meaning, no existing design needs editing, and every
example from M1–M7 keeps building byte-identically until it is deliberately given a
stackup that says otherwise. The brief asked to be consulted before breaking the
schema; nothing here breaks it, so there is nothing to consult about.

`prefer_layers` is reused rather than renamed to the brief's `layer_pref`, because
it already exists, is already documented, and already appears in three example
designs. `layer_forbid` is added as the brief asks.

## Consequences

* **Rip-up costs nothing, geometry costs a lot.** The negotiation runs on symbols;
  geometry is produced once, at the end, from the settled topologies. That inverts
  M7c's loop and is what makes iteration affordable.
* **A plane layer holds no copper** until pours exist. Declared, respected, empty.
* **Determinism is unchanged.** No randomness is introduced anywhere; every
  ordering has an explicit final tie-break on the route key, and the negotiation
  schedule is fixed. Shuffling the input order must converge to *a* legal result,
  not necessarily the same one — but a given source always produces the same board.
* **The failure mode is honest.** A board that will not converge reports which nets
  own the contested cuts, including any `rip_up: never` net that blocked it, rather
  than silently emitting an over-subscribed board. It also stops early: three passes
  without fewer over-subscribed cuts means over-subscribed rather than unlucky.
* **A net's own pads now block it**, except the two it is landing on. Copper may
  legally overlap its own net, but a track that clips a pad it is only passing
  leaves a crescent a few microns wide, which KiCad reports as a copper sliver and a
  fabricator would rather not etch. The repair pass lifts the restriction when the
  alternative is not connecting at all — which is exactly the situation a
  receptacle's four shield tabs create.
* **Keepouts are now routing law too.** `layout.placement.keepouts` existed in the
  format from M1 and no router had ever read it. The layered field has to, because a
  keepout is exactly the kind of obstacle a via column must respect, and once one
  half of the router honours it the other half must as well — so the per-net
  geometry and `route check` read it too. A design that declared a keepout and
  watched copper run through it now gets what it asked for.
* **Two pre-existing bugs surfaced**, both invisible until a footprint was rotated,
  and both fixed here because the four-layer example is the first board to rotate
  one. KiCad stores a pad's angle *absolutely* — it already includes the footprint's
  rotation — so a generated copy that leaves the library's angles alone describes a
  part whose pads did not turn with it. And KiCad's rotation is counter-clockwise as
  drawn, in a coordinate system with Y pointing down, so the obstacle extractor's
  textbook rotation was the mirror of it. Either one alone puts pin 1 where KiCad
  draws pin 2, and the router then lands copper on the neighbouring pad.

## Sources

* McMurchie, Ebeling, "PathFinder: A Negotiation-Based Performance-Driven Router
  for FPGAs", FPGA 1995 — <https://dl.acm.org/doi/10.1145/201310.201328>
* Dai, Dayan, Staepelaere, "Topological routing in SURF: generating a rubber-band
  sketch", DAC 1991 — <https://dl.acm.org/doi/pdf/10.1145/127601.127622>
* Dai, Kong, Sato, "Routability of a rubber-band sketch" —
  <https://www.semanticscholar.org/paper/103a2d8b33ce3de0710f3ab11de7f2f9c25947fd>
* Maley, *Single-Layer Wire Routing and Compaction*, MIT Press, 1990 —
  <https://mitpress.mit.edu/9780262132503/single-layer-wire-routing-and-compaction/>
* Erickson, "Shortest (Homotopic) Paths" —
  <https://jeffe.cs.illinois.edu/teaching/compgeom/notes/05-shortest-homotopic.pdf>

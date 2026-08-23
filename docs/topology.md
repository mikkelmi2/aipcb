# Topological routing

A route in `aipcb` is stored as **topology**, not geometry: which obstacles it
passes, on which side, and where it changes layer. Geometry is derived from that,
every build, against the current placement.

The reasoning behind this — and the prior work it comes from — is in
[ADR 0006](decisions/0006-routing-approach.md) for the single-layer machinery and
[ADR 0007](decisions/0007-multilayer.md) for the multilayer router built on top of
it. This document describes the model itself and how it becomes copper.

## Why not store coordinates

Because coordinates are wrong the moment anything moves.

```
        stored as geometry                     stored as topology
   ┌──────────────────────────┐          ┌──────────────────────────┐
   │  U1 ●────┐               │          │  U1 ●────┐               │
   │          └────● R3       │          │          └────● R3       │
   │      ▓▓▓▓                │          │      ▓▓▓▓                │
   │       C7                 │          │       C7                 │
   └──────────────────────────┘          └──────────────────────────┘
              C7 moves 2 mm right                    C7 moves 2 mm right
   ┌──────────────────────────┐          ┌──────────────────────────┐
   │  U1 ●────┐               │          │  U1 ●──┐                 │
   │        ▓▓╪▓▓──● R3       │          │      ▓▓▓▓└───● R3         │
   │          C7              │          │       C7                 │
   │   the track now runs     │          │   "pass C7 on its left"  │
   │   through the capacitor  │          │   still means something  │
   └──────────────────────────┘          └──────────────────────────┘
```

The sketch survives because it never said *where* the wire was — only what it went
around. Re-tightening is then cheap, and it is the same tightening that produced
the geometry in the first place.

## The model

```yaml
layout:
  routes:
    - net: USB_DP
      from: J1.3          # a pad: REFDES.PAD
      to: R1.1
      layer: F.Cu
      passes:
        - obstacle: J1.4   # a pad, a component (`U1`), or a via (`via:v1`)
          side: left       # looking along the direction of travel
          reason: Keeps the pair on the outside of the connector's fan-out.
        - kind: via
          to_layer: B.Cu
          name: v1         # so another route can name it as an obstacle
      reason: Impedance-controlled, so it stays on one layer with the plane below.
```

| Field | Meaning |
|---|---|
| `net` | The net this route belongs to. |
| `from`, `to` | The pads it connects. A net with *n* pads needs *n−1* routes. |
| `layer` | The layer it starts on. |
| `passes` | Obstacles passed, **in order of travel**. Empty means a direct run. |
| `reason` | Why the route goes this way. |

A `passes` entry is either a **pass** (`obstacle` + `side`) or a **via hop**
(`kind: via` + `to_layer`, optionally `name`).

### Sides are relative to travel

`side` is `left` or `right` as seen looking from `from` towards `to`.

```
        from ●───────────────────────▶  to
                       ▲
                 left  │  ▓▓▓▓  ← "side: right"
                       │   C7
```

Absolute directions were rejected: they are not invariant under rotating or
mirroring the board, and they read wrongly the moment a route doubles back on
itself. Travel-relative sides always mean the same thing.

## From sketch to copper

```
  sketch          placement            free space           homotopy         copper
 (durable)        (this build)         (this build)         (this build)   (this build)

 pass J1.4 ──►  pads, bodies,   ──►   board polygon   ──►  crossing    ──►  tightened
   on left      board outline          minus inflated       sequence         polyline
                                       obstacles            + sleeve       → segments
```

Each step, and why it is that step:

**1. Inflate the obstacles.** Every pad, body and already-routed track becomes a
convex polygon grown by `max(this net's clearance, the other net's clearance)` plus
half this route's track width. KiCad enforces the larger of two nets' clearances, so
using only the routing net's figure leaves violations short by exactly the
difference.

Inflating first is the trick the whole approach turns on: the shortest path through
what remains is *automatically* a legal path. Clearance is satisfied by
construction, not checked afterwards and patched.

**2. Triangulate what is left.** A constrained Delaunay triangulation of the board
polygon minus the obstacles, from Shapely/GEOS. The interior edges of that
triangulation — the *diagonals* — are the alphabet the topology is written in.

The board polygon is the *real* one, holes included. `board:` (M9) can declare an
outline with arcs and any number of cutouts, and the router reads them back off the
board's own `Edge.Cuts` graphics: the loops are chained, the largest becomes the
boundary and the rest become holes in the free space. That last part matters more
than it sounds. A cutout is not an obstacle sitting in the middle of a corridor — it
is a hole in the plane the corridors are cut from, so two points either side of a
slot are in genuinely different homotopy classes, and going round it one way is a
different route from going round it the other. The topology model needed no change
for that; it needed to be told about the hole.

The board edge is eroded by the source's `edge_clearance` before anything is
triangulated, and the same figure is written into the project as KiCad's
`min_copper_edge_clearance`, so the router and DRC are checking one number rather
than two that happen to agree.

**3. Walk it.** The route is forced through a point just off the named side of each
obstacle it must pass, and the legs between those points are found by A\* over
portal midpoints. Where the walk went *is* the homotopy class.

**4. Reduce.** Crossing the same diagonal twice in a row means stepping over it and
straight back, which no shortest path does. Cancelling adjacent repeats gives a
canonical form for the class.

```
  raw:      A B C D D D E F H L K J J K L M N
  reduced:  A B C D E F H M N
```

**5. Tighten.** The reduced sequence defines a **sleeve** — a strip of triangles
that is topologically a disk even where it revisits triangles geometrically. The
funnel algorithm walks it maintaining an apex and two concave chains, emitting a
corner whenever the chains cross:

```
                    ┌── left chain ──┐
        apex ●══════╡                ╞═══ current diagonal
                    └── right chain ─┘

   the path bends only where a chain vertex becomes the new apex
```

Each diagonal costs amortised constant time, so tightening is linear in the length
of the sleeve.

## Layers

Every copper layer gets its own triangulation, over the obstacles that are actually
on it: pads that reach it, the board edge, keepouts naming it, any via barrel that
passes through it, and any copper a previous build preserved. A via is a **column** —
a node that is an obstacle on every layer its barrel passes, whether or not it
carries signal there, and that joins the triangulations of the layers it connects.

```
        F.Cu   ───────●──────────────   carries the signal, and joins the layer below
                      │
        In1.Cu ───────╫──────────────   a plane: closed to signals, and the barrel
                      │                 still goes straight through it
        In2.Cu ───────╫──────────────   so does this one
                      │
        B.Cu   ───────●──────────────   the other end of the hop

                one via, four obstacles
```

A layer the stackup gives over to a plane is **routing law**, not decoration:
nothing signal-carrying is placed there, and a net class reaches one only by naming
it in `prefer_layers`. Pouring the plane is a later milestone; declaring one today
reserves it.

### A cut has a capacity

A *cut* is a segment across the free space, and Maley's criterion says a set of
routes can be turned into legal geometry exactly when no cut is over-subscribed:

```
capacity(cut) = length(cut) + reference clearance
demand(net)   = track width + clearance
```

That makes "does this board fit" a local question, checkable before any geometry
exists — and, crucially, an *undoable* one. Ripping a route up subtracts its demand
and nothing else, which is what makes the negotiation below affordable.

**Which cuts, though?** Maley quantifies over all of them; a triangulation offers
the ones it drew. Two families are charged here:

* **Every interior edge of the triangulation.** The obvious ones.
* **The second diagonal of every convex adjacent triangle pair** — the segment
  joining the two far vertices of the quadrilateral two triangles make. A wire that
  enters a triangle through its other two edges never crosses the triangulation's
  own diagonal at all; it rounds the apex, and the room it has to do that is this
  segment. The CDT declined to draw it, which is exactly the case where it is the
  shorter one: on the bundled examples, 4–29% of diagonals per layer have a shorter
  partner. [ADR 0014](decisions/0014-special-cuts.md) has the measurement.

Even together those are a **subset**, so the answer is a *lower bound on
congestion* rather than the criterion in full: a segment between two obstacle
vertices spanning more than one triangle pair is a cut nothing charges. A clean
capacity report is evidence, not proof. Legality does not rest on it — the
stretcher builds each route inside free space that already excludes every other
net, and a final invariant asks the finished board whether two nets overlap — so
what the remaining gap costs is a route that fails or detours somewhere the report
called comfortable, never a short circuit.

`aipcb route check` reports over-subscribed cuts across every layer, naming the
nets that share them and which kind of cut it was. The router's own cost model is
charged for the triangulation's diagonals only; charging it for the second
diagonals as well changes routing decisions, and that is a trade the
[roadmap](roadmap.md) requires to be measured on `aipcb bench` before it is made.

## Routing a whole board

Tightening one route is pure geometry. Routing a board is not, and the work splits
in two.

**Negotiation is symbolic.** Which layer a connection uses, where its vias go and
which corridor it takes are decided on the shared layered field, where a route is a
set of subscriptions against cut capacities. This is PathFinder (McMurchie &
Ebeling, 1995): every net is routed as though it had the board to itself, nets are
*allowed* to share a corridor illegally, then the price of over-used corridors goes
up, the nets that lost the argument are ripped up and re-routed, and it repeats
until no cut is over budget. A net gives up a corridor only when the corridor is
genuinely contested, and the amount of contention — not the routing order — decides
who keeps it. Every pass is logged.

**Realization is geometric.** Once the topologies have settled, each connection is
tightened into copper in priority order, and each finished route becomes an obstacle
for the ones after it. That feedback is what keeps two routes sharing a corridor
from landing on top of each other: the cut criterion promises they *fit* side by
side, and the sequential rubber-band pass is what actually puts them there.

The details that matter:

* **What to connect.** A net's pads are joined by a minimum spanning tree. Pad
  *instances*, not pad numbers — a SOT-223's pin 2 and its thermal tab are both
  "pad 2" and both on the output net, but they are separate copper and KiCad
  rightly reports them unconnected until a track joins them.
* **In what order.** By `priority`, then by difficulty: length times congestion,
  where congestion before anything is routed is the narrowest cut a route leaving
  either pad has to get through. A pair escaping a fine-pitch connector scores
  enormously and chooses first; a ground hop between two capacitors in open board
  scores near nothing and goes last. Priority defaults are in
  [`routing-costs.md`](routing-costs.md); a net class can state its own.
* **Feedback.** Each finished route is fed back as obstacles — *one per segment*,
  never one hull over the whole polyline, which would swallow the entire area
  inside every bend. Vias are fed back on every layer their barrel passes.
* **Vias settle.** The search picks a via's *pocket* from a discrete set of sites;
  it does not optimise a continuous coordinate, which would buy nothing a cheaper
  pass cannot buy afterwards. Once both legs are tightened, each via is pulled
  towards the straight line between its neighbouring corners and the legs are
  re-tightened against it, keeping the move only if the pair came out shorter and
  both are still legal.
* **Repair.** The shared field is an approximation: obstacles inflated by the widest
  clearance on the board, and no knowledge of where copper eventually landed. When a
  connection cannot be realized against the board as it actually stands, it is
  re-routed on a field built for that net alone — its own clearance, its own width,
  its own pads not counted as obstacles. Only failures pay for the second field.
* **A route joins its own net where it meets it.** A net's own copper is not an
  obstacle, or two connections of one net could never meet; the cost of that freedom
  is that a later connection will happily run the length of an earlier one before
  branching off. Electrically it is finished the moment it touches, so the duplicate
  is trimmed away.

## Escaping a dense package

A fine-pitch package is the one place where none of the above helps. A QFN-32 on a
0.5 mm pitch leaves about 0.25 mm between neighbouring pad edges; a 0.25 mm track
with 0.2 mm clearance needs 0.65 mm of corridor. There is no corridor. That is a
fact about the geometry, not a weakness of the search, and no amount of negotiation
finds a path through a wall.

So M9e solves it the way a layout engineer does — with a *pattern*:

```
   pad field            escape pattern             what the router sees
  (impassable)        (generated, fixed)              (ordinary board)

  ▪ ▪ ▪ ▪ ▪            ▪─○   ▪──○                      ○   ○
  ▪       ▪     ──►    ▪─○   ▪──○           ──►      ○       ○
  ▪ ▪ ▪ ▪ ▪            stub + via, staggered           terminals in open board
```

The generator runs before anything is routed. It lays a stub from each connected
pad to a via just clear of the part, registers that copper as a fixed obstacle, and
publishes the via as a *terminal* in place of the package pad. Everything after that
is the machinery already described: the escape terminals are pads as far as the
field, the search and the stretcher are concerned.

Three details carry the weight:

* **Staggering.** Neighbouring escapes alternate between two rows, decided by where
  the pad sits rather than by where it comes in a list. A single row of 0.45 mm vias
  on a 0.5 mm pitch is one continuous piece of copper.
* **The whole escape is checked, not just the via.** A via somewhere clear is easy;
  the stub reaching it has to clear the package's own pads, its courtyard, the board
  outline and every cutout. A pad with nowhere to go is reported and left to the
  router rather than given copper DRC will reject.
* **Escapes come back out if the router did not use them.** The generator has to
  propose one per pad before routing; where a route reached the pad without ever
  using the far layer, the via would join copper to nothing, so it is removed.

This is deliberately *not* a second router — see
[ADR 0008](decisions/0008-mech-placement.md#rejected) for why a foreign autorouter
is rejected rather than deferred. It is the first tenant of a pattern-generator
architecture; the candidates for the next are in [`roadmap.md`](roadmap.md).

## Handing over

The router refuses rather than delivering marginal geometry. When a connection
cannot be tightened into legal copper — the corridors it needs are over-subscribed,
or the plan will not realize against the board as it now stands — it is *handed
over*: no copper, and an entry in `handed_over` saying which of three things went
wrong (`over_complexity`, `no_path`, `unrealizable`), which corridors it depended
on, how wide each is, how much copper wants to cross it, and which nets those are.

`aipcb route all --json` and `aipcb check --json` both carry the list. A hand-routed
replacement is preserved by the incremental build and treated as law on the next
run. The acceptance bar does not move: zero DRC violations on whatever *is* routed,
plus an explicit account of what is not.

## Checking a sketch

```bash
aipcb route check design.yaml
```

`route check` decides whether each sketch is realizable *on this placement*. It
checks the things that can be settled by inspection — the net exists, the endpoints
are pads on it, the obstacles are on the board, the layers exist on this stackup —
and then decides realizability the only way it can be decided: by building the
homotopy class against the actual free space. A class that cannot be built is one
that does not exist.

```
design.yaml:118:5: error[route-unrealizable]: route USB_DP/J1.3>R1.1 cannot be
built as sketched: waypoint 1 is not in the routable area
  at: layout.routes[0]
  hint: the point falls inside another part's clearance, so the route as sketched
        cannot exist; pass on the other side, or move the parts apart
```

It also asks the question no individual route can answer — whether they all fit
*together*. Every cut has a capacity, and a set of sketches that over-subscribes one
cannot be built however sound each of them is on its own. Silence from this check
is a lower bound rather than a guarantee, for the reason [above](#a-cut-has-a-capacity):

```
design.yaml: error[route-cut-over-subscribed]: X1, X2 together need 0.90 mm of a
corridor on F.Cu that is 0.85 mm wide
  at: layout.routes
  hint: one of them has to go somewhere else: another layer, another side of the
        obstacle between them, or a different placement
```

## Generating routes

```bash
aipcb route all design.yaml --out build/
```

Explicit sketches are honoured; every other connection is negotiated across the
stackup's signal layers. Unrouted connections are reported with a reason rather than
being fatal — a partly routed board is a useful thing to open in KiCad and finish by
hand.

```bash
aipcb route all design.yaml --layers F.Cu     # would one layer have done?
aipcb route all design.yaml --congestion 0    # route for length alone
```

Running it twice is safe: the router recognises its own copper by UUID and replaces
it, while anything it did not generate — a hand-routed pair, a poured zone — is left
alone and treated as a wall.

## Differential pairs

A pair is not two nets that happen to run alongside each other — its impedance
comes from the coupling between them. So a pair is tightened **once**, as a single
centre-line wide enough for both traces and the gap between them, and the result is
offset to either side:

```
   tighten one centre-line at 2w + gap            offset by ±(w + gap)/2
   ────────────────────────────────────           ──────────────────────
                                                   ══════════════════════  DIFF_P
        ══════════════════════════════                                     gap
                                                   ══════════════════════  DIFF_N
```

The gap is then correct everywhere by construction, including around corners, where
Shapely's mitring handles the inside cutting in and the outside swinging wide. The
two halves come out the same length except for that corner difference — which is
the skew a real pair accumulates, and is measured against the class's `max_skew_mm`
rather than assumed away. On the `diff-pair` example the halves come out at
43.00 mm each, with 0.000 mm of skew.

If a net class states an `impedance_ohm` but no `diff_pair_gap_mm`, the gap is
estimated from the standard edge-coupled microstrip formula and **reported as an
estimate**. When the target is not reachable by spacing at all — a 0.34 mm trace on
a 0.7 mm dielectric can only span about 102–195 Ω however far apart the halves go —
that is said plainly, because the answer is to change the trace width or the
stackup, and clamping to the nearest gap would hide it.

The fan-out at each end — where the two halves leave the pair's own pitch and go to
pads that are somewhere else — is **tightened too**, as an ordinary short route
between two fixed points, so it goes round what is in its way instead of through it.
It also necks down to the class's ordinary trace width, because it is not coupled to
anything and because a 0.34 mm pair trace cannot land on a 0.65 mm-pitch receptacle
pin at all. Necking a pair down at the connector is what a person does, for the same
reason.

### Length matching

Skew is length, so closing it means adding length. When a pair misses its
`max_skew_mm`, the shorter half is meandered until it does not:

```
        before                                after
   ═══════════════════════            ═══════════════════════
   ═════════════════                  ═════════╱╲╱╲╱╲════════
        0.9 mm short                      matched to 0.005 mm
```

Three rules shape what gets built, and all three are about not making the board
worse to fix a number. The meander stays **in the net's own corridor**, checked
against the same free space the route was tightened in — one that does not fit is
not built. It goes in the **fan-out, never the coupled run**, because folding one
half of a coupled pair destroys the property the pair exists for. And the length
being matched is the **whole conductor, via barrels included**: on a 1.6 mm board a
through via is about 1.5 mm of copper, ten times a fast pair's budget.

When there is no room, that is said plainly rather than approximated away.

### When a pair cannot be coupled

Coupled routing checks its own output and falls back to routing the halves
separately rather than shipping something that only looks like a pair. It refuses
when:

* the net has other than exactly two pads per half, so its two ends are ambiguous;
* the pads swap sides between the ends, which needs a crossover — see below;
* more than a third of each half would be fan-out, meaning the end pads are too far
  apart for the run between them to be coupled at all;
* the two halves would come closer than their width and gap allow, which is measured
  and reported as a number;
* the fan-out to the pads would not clear something, which is reported by name.

Each refusal names the pair and what to change.

### Crossovers, and why a paired via is not one

A pair whose pads swap sides between its two ends has to cross over somewhere, and
the obvious mechanism does not work. Two parallel wires on one layer form a
two-strand braid, and exchanging them requires exactly one crossing; moving both
halves to another layer at a paired via column is a *translation*, not a
transposition, so it cannot absorb that crossing. Whichever layer the pair is on
afterwards, the two halves still have to cross on it. This is planarity, not an
implementation gap.

The mechanism that does work is one half diving to the adjacent layer, passing under
the other, and coming back — two vias on one net, and a real impedance
discontinuity. That is not built. A pair that needs a crossover is reported, with
the observation that swapping the two pads at one end removes it, which on a board
where both ends are your own components is usually the better fix anyway.

### Pairs stay on one layer

A pair's two halves would have to change layer *together* to keep their gap, and a
paired via column is not two vias at the pair's own pitch: a 0.6 mm via at a 0.54 mm
pitch is one piece of copper. The halves would have to splay out to via pitch and
back, which is a distinct pattern with its own discontinuity. Not built, so a pair
that cannot be routed on one layer says so.

## What is not built yet

* **Copper pours.** A layer declared a plane is respected and left empty; nothing
  fills it. Until that exists, a plane's own net is routed as tracks on the signal
  layers like anything else.
* **Pair crossovers** and **pairs changing layer**, both for the reasons above.
* **Necking a single-ended track down** to reach a pad it is too wide for. A pair's
  fan-out necks; an ordinary route does not, so a 0.6 mm power track cannot land on
  a 0.65 mm-pitch pin and says so. The fix in the source is a narrower class for
  that net, which is what the `usb-port` example does.
* **`side: back` placement**, which is validated and then warned about — the router
  is ready for it, the placer is not.
* **Pouring a plane around an exposed pad's thermal vias.** M9e drills them; there
  is no copper on the far side for them to land in until zone pours exist.
* **Arcs** at hull corners. Rubber-band tightening naturally produces any-angle
  segments, which is what SURF and TopoR emit and what every fab accepts; arcs are
  a refinement, not a correctness matter.

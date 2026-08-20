# Topological routing

A route in `aipcb` is stored as **topology**, not geometry: which obstacles it
passes and on which side. Geometry is derived from that, every build, against the
current placement.

The reasoning behind this — and the prior work it comes from — is in
[ADR 0006](decisions/0006-routing-approach.md). This document describes the model
itself and how it becomes copper.

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

## Routing a whole board

Tightening one route is pure geometry. Routing a board is not, because every route
that lands is an obstacle for the ones after it.

* **What to connect.** A net's pads are joined by a minimum spanning tree. Pad
  *instances*, not pad numbers — a SOT-223's pin 2 and its thermal tab are both
  "pad 2" and both on the output net, but they are separate copper and KiCad
  rightly reports them unconnected until a track joins them.
* **In what order.** Critical nets first (differential pairs, then high-speed, then
  signal, then power and ground), and within a class the shortest first. This is
  what a person does and for the same reason: a pair escaping a fine-pitch
  connector has almost no freedom, while a ground hop between two capacitors has
  plenty. Ordering purely by length costs six connections on the USB example.
* **Feedback.** Each finished route is fed back as obstacles — *one per segment*,
  never one hull over the whole polyline, which would swallow the entire area
  inside every bend.

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

## Generating routes

```bash
aipcb route all design.yaml --out build/
```

Explicit sketches are honoured; every other connection gets the shortest topology
the triangulation allows. Unrouted connections are reported with a reason rather
than being fatal — a partly routed board is a useful thing to open in KiCad and
finish by hand.

## What is not built yet

* **Via hops** are modelled and validated but the stretcher rejects them, so every
  route is single-layer for now. The model is in place because it must be: adding
  it later would change the source format.
* **Differential pairs** are declared and carry their impedance and skew budgets,
  but coupled tightening and meander insertion are M7d.
* **Arcs** at hull corners. Rubber-band tightening naturally produces any-angle
  segments, which is what SURF and TopoR emit and what every fab accepts; arcs are
  a refinement, not a correctness matter.

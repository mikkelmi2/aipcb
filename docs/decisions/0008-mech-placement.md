# 0008 — The board boundary as source, mechanical placement, fanout, and honest failure

* **Status:** Accepted
* **Date:** 2026-08-21
* **Context:** milestone M9 (M9-outline, M9a–M9f), building on
  [ADR 0004](0004-board-generation.md), [ADR 0005](0005-incremental-builds.md),
  [ADR 0006](0006-routing-approach.md) and [ADR 0007](0007-multilayer.md)

## Context

Up to M8 the board's *edge* has been an afterthought and its placement model has
been purely relative. Both assumptions are false on any board that goes inside
something.

Real boards have a mechanical boundary that is given, not derived: an outline that
is frequently not a rectangle, cutouts for flex tails and fixings, and components
whose positions are dictated from outside the electrical design — a connector
aligned to an enclosure opening, mounting holes on a bolt circle, a button under a
cap, an LED under a light pipe. Those are boundary conditions. An auto-placer that
treats them as preferences produces a board that cannot be assembled.

This ADR records what M9 changes and, as importantly, what it deliberately does
not.

## What the code assumed before M9

Written down first, because every item is a place the new model has to reach.

**The outline is a rectangle, or is treated as one.**

| Place | Assumption |
|---|---|
| `model.layout.BoardOutline` | `shape: rect` with `width_mm`/`height_mm`, or a bare point list. No arcs, no holes. |
| `compile.board._outline` | Emits `gr_line` between consecutive corners. Arcs are unrepresentable. |
| `compile.board._auto_outline` | Invents a rectangle around whatever was placed. |
| `compile.place.usable_area` | Reduces any outline to `(width, height)` — for a polygon, to its **bounding box**. On an L-shaped board that is the whole missing corner. |
| `route.obstacles.board_outline` | Recovers the edge as the **convex hull** of every `Edge.Cuts` point. Conservative for a convex board; wrong for a concave one, and blind to holes. |
| `route.triangulate.free_space` | `ShapelyPolygon(environment.outline)` — a single ring, no holes. |
| `checks.mapping.build_index` | Indexes exactly four edge-segment UUIDs, because a rectangle has four sides. |

**Every component is movable.**

| Place | Assumption |
|---|---|
| `compile.place.plan_placement` | Shelf-packs *every* component. The only exception is `layout.placement.rules[].region_mm`, which pins a group into a box and then leaves it out of the packer. |
| `compile.place._place_regions` | Region-pinned parts are not fed back to the packer as occupied space, so a packed cluster may legally land on top of one. |
| `compile.preserve.component_fingerprint` | Hashes `side`/`region_mm`/`orientation_deg` from the layout rules. Nothing else can pin a part, so nothing else is hashed. |
| `route.plan` | Terminals are *pads*. A package's escape geometry is not a concept the router has. |

**Edge clearance is a constant.** `route.obstacles.EDGE_CLEARANCE = 0.5` is
hard-coded and used in three separate places (`field.build_field`,
`geometry.geometry_for`, `check._layer_geometry`). The generated `.kicad_pro`
does not state an edge-clearance rule at all, so KiCad checks against its own
default and the two figures are related only by luck.

**Failure is a warning.** `route.plan._fail` records an `Unrouted` with a free-text
reason and reports a `route-failed` warning. There is no machine-readable category,
nothing says *where* the board ran out of room, and the negotiation's own
"congestion did not settle" outcome does not stop the router from realizing the
marginal geometry it settled on anyway.

## Decisions

### 1. The outline is a first-class source object, in its own reference frame

A new top-level `board:` block owns the boundary — outline, cutouts, edge
clearance, and the coordinate convention. It sits beside `layout:` rather than
inside it, because it is mechanical law rather than layout intent, and because
changing it *should* be a big diff.

**The source frame is Y-up, with the origin at the board's bottom-left corner.**
KiCad's board space is Y-down. The conversion is one function in one module
(`compile.frame`), and it is stated in the format documentation rather than left
to be inferred:

```
kicad_x = origin_x + (x - min_x)
kicad_y = origin_y + (max_y - y)
```

The M7 postmortem's lesson is that a sign convention which is "obviously right" is
the class of bug that ships. So this one has an explicit regression test with a
deliberately asymmetric outline: a board whose north and south edges differ, whose
Y orientation cannot come out right by accident.

`layout.origin_mm` keeps its meaning — the KiCad position of the board's top-left
corner — so a rectangle emits exactly the bytes it emitted before M9.

**Ring canonicalisation.** A closed ring is emitted starting from its lowest
`(x, y)` corner in KiCad coordinates, wound so the signed area is positive. This
is what makes the migration byte-clean: the Y-up rectangle
`[(0,0), (w,0), (w,h), (0,h)]` canonicalises to exactly the corner order M3 has
always written. It also means rotating the point list in the source does not churn
the diff, which is worth having on its own.

**The legacy blocks keep their meaning.** `layout.outline` still works and still
means what it meant; declaring both it and `board:` is an error rather than a
silent precedence rule. `layout.placement.rules[].region_mm` stays in the old
frame (Y-down, relative to `layout.origin_mm`) because changing it would move
every part on four of the bundled examples. The new `placement:` block is the
supported way to say where something goes, and it is in the board frame.

### 2. Propagation is the work; `Edge.Cuts` export is not

Emitting the edge is a page of code. Making the rest of the chain believe it is the
milestone:

* **Reading it back.** `route.obstacles.board_outline` is replaced by a real ring
  reconstruction: `Edge.Cuts` lines and arcs are chained into closed loops, the
  largest becomes the outer boundary and the rest become holes. The convex hull is
  gone. This matters even for hand-drawn edges, which is how most boards get one.
* **Free space with holes.** `RoutingEnvironment` gains `cutouts` and
  `edge_clearance`; `free_space` builds `ShapelyPolygon(outline, holes=cutouts)`.
  Nothing else in the topology model changes — a hole is an obstacle that happens
  to be a hole, and the homotopy machinery already distinguishes going round an
  obstacle on one side from going round it on the other. That is the whole reason
  ADR 0006's model was worth building.
* **Packing inside a polygon.** The shelf packer keeps its shape but tests each
  candidate box against the real usable area (outline − cutouts − margin −
  anchored parts) instead of against a width and a height. The natural cursor
  position is tried first, so a rectangular board packs exactly as before.

### 3. Three levels of placement, and `fixed` outranks everything

```
fixed  >  edge / region  >  relative intent (groups, proximity, keep-apart)
```

`fixed` parts are placed first and are immovable. `edge` and `region` parts are
placed by the packer but projected back into their allowed set. Relative intents
then pull movable parts toward whatever anchors their cluster contains — a group
containing a fixed connector packs *around* the connector rather than being
shelf-packed somewhere else. No new mechanism was needed for that: the union-find
clustering already puts a decoupling capacitor in the same cluster as its IC, so
an anchored cluster is simply a cluster with a fixed origin.

A relative intent naming a fixed part constrains only the other parts. The group
deforms around the anchor; the anchor never moves.

`reason:` on a `fixed` placement is lint-warned when absent, for the same reason
cutouts carry one: an agent reading the source has to be able to tell mechanical
law from a preference somebody typed once. A missing reason is a warning, not an
error, because a mounting hole with `role: mounting_hole` explains itself.

### 4. Conflicts are caught in `validate`, before anything is built

Placement conflicts are cheap to detect and expensive to discover in KiCad. `aipcb
validate` gains six checks: overlapping fixed courtyards, a fixed part outside the
real outline polygon, a courtyard over a cutout, an `edge`/`region` whose allowed
set is empty, and a conservative bound check on relative intents against the
anchors.

The last one is deliberately **conservative and a warning**. Exact infeasibility of
a set of distance constraints is not a cheap question, and a validator that
sometimes says "impossible" about something achievable is worse than useless. So
it reasons with intervals — the closest two allowed sets can possibly be — and
reports only when that lower bound already exceeds the constraint.

### 5. Manual adjustment is reconciled, not fought

A `fixed` part nudged in KiCad is a *conflict between two truths*, and the tool
should say so rather than pick a side silently. `aipcb build` reports the drift;
`aipcb sync-placement` writes the KiCad position back into the YAML, in place,
preserving the file's formatting and comments. The source stays the single truth —
the tool just helps you update it.

Movable parts keep M6's preserve behaviour unchanged, because for them there is no
second truth to reconcile: the source never said where they go.

### 6. Fanout is a pattern generator, not a second router

Escape routing from a dense package is not a general routing problem. It is a
geometric *pattern* — dog-bone vias in the quadrant a BGA ball points toward,
short outward stubs from a QFN's perimeter — and the right way to build it is a
deterministic generator, not a search.

The architectural rule is what matters here: **fanout runs before routing, and its
output is fixed obstacles plus terminals.** The generator lays the stub and the
via, registers that copper as an obstacle, and publishes an *escape terminal* into
the routing environment in place of the package pad. The rubber-band router that
runs afterwards has no idea a fanout happened; it sees pads at the escape points
and routes between them exactly as it always has.

That is also why an external autorouter is rejected rather than deferred — see
below. The fanout generator is the demonstration that pattern generators compose
with this architecture, and a foreign router is the demonstration that they do not.

Vias are keyed by **pad instance**, never by pad number. A USB receptacle has four
pads numbered 6 and a QFN has an exposed pad split into a grid; M7 already learned
this lesson and paid for it in shorted nets.

### 7. Refusal is a first-class outcome

The differential-pair work established that refusing with a reason beats delivering
something that looks like a pair and is not. M9 generalises it to capacity.

When the negotiation cannot converge, the connections still sitting on
over-subscribed cuts are **handed over** rather than realized: marked
`unrouted: over_complexity`, with the layer, the location of the cut that blocked
them, its width, the demand on it, and which nets own the contested capacity. The
same applies to a topology that cannot be tightened DRC-clean within the budget.

`aipcb check --json` and `aipcb route all --json` both list them, so an agent can
react — raise the layer count, move a part, change a priority — and a human knows
exactly which nets to finish by hand. A net routed by hand afterwards is preserved
by M6 and is law on the next run.

The acceptance bar does not move: zero DRC violations on whatever *is* routed, plus
an explicit, machine-readable list of what is not.

## What else had to move

Four things this milestone did not set out to change, and changed because the work
made them wrong.

**`aipcb check` now routes.** A DRC pass over a board with no copper on it has
checked almost nothing, and the question an agent actually has — "is this design
buildable" — is not answerable without trying. So `check` builds, routes, and then
runs ERC and DRC, with `--no-route` for the cases that only want the old behaviour.
What the router will not deliver legally comes back as the hand-over list rather
than as marginal geometry, so the DRC result stays meaningful. This is also what
makes `aipcb check --json` able to list handed-over nets at all.

**The project file states the board's real constraints.** KiCad's own defaults
include a 0.5 mm minimum via and a 0.3 mm minimum drill. A 0.5 mm-pitch package
cannot be escaped with either, so a design whose net classes ask for something
tighter has those minimums written into `.kicad_pro` — and only those, so a board
built to comfortable rules keeps a project file byte-for-byte identical to the one
it had before. Same reasoning as `edge_clearance`: the router and DRC should be
checking one number, not two that happen to agree.

**Schematic cells are sized to their symbols.** Every pin gets a stub and a label,
and the sheet grid was a fixed 50.8 × 45.72 mm. A 33-pin MCU symbol is 76 mm tall,
so its pins landed on top of the row below's stubs — and KiCad, which connects by
exact coordinate, joined them. VCC shorted to GND, and ERC said so. Cells now grow
to fit the biggest symbol on the sheet, never shrinking below the old figure, so
every design of ordinary parts lays out exactly as it did.

**Two barrels through one piece of copper are one hole.** A fanout escape via that a
route continues from is recorded by both the escape and the route, and the router
occasionally lands its own via a tenth of a millimetre from one. Both cases are a
fabricator being asked to drill twice through the same copper. The emitter writes
each hole once, and vias of a net whose holes physically overlap are merged onto the
earlier one — which is safe precisely because the destination already holds a legal
via of the same net and the same size. Neither is a routing decision; both are the
board saying what it means.

## Rejected

**Integrating an external autorouter (Freerouting or similar).** Rejected, not
deferred, and recorded here so it is not relitigated.

A foreign router breaks three properties this toolchain is built on. *Determinism*:
Freerouting's output depends on a time budget and an internal random seed, so the
same source produces different boards. *Source mapping*: its copper arrives as
geometry with no relationship to the source that asked for it, so a DRC violation
on one of its tracks cannot be reported against a line in a YAML file — which is
the entire point of ADR 0004's UUID mapping. *Preserve semantics*: M6 tells our
copper from a human's by UUID, and copper from a third tool is neither.

The fanout generator shows the alternative that is open to us: a *specific*
generator for a *specific* pattern, deterministic, UUID-mapped, and composing with
the topology model rather than replacing it. Where the router is not good enough,
the answer is another pattern generator or an honest hand-over — not a black box.

**Making the whole placement model absolute.** Tempting, and wrong. Relative intent
is what makes a design portable between board revisions; absolute coordinates are
what makes it match an enclosure. A board needs both, and the layering above says
which wins where.

## Out of scope, and why

Recorded in `docs/roadmap.md` as future work:

* **MCAD import** (`aipcb import-mech`, reading DXF/STEP reference points into
  `fixed:` blocks). The YAML is designed so this becomes a pure generator later —
  a `fixed:` block with a `reason:` pointing at the mechanical file is exactly what
  an importer would emit — but nothing is built.
* **3D clearance and height checking** against an enclosure model.
* **Back-side placement mirroring.** `side: back` still validates, warns, and
  places on the front. Mirroring means swapping every `F.`/`B.` layer pair in a
  footprint, and approximating it is worse than refusing it.
* **Panelization** — mouse bites, V-cut, rails. Fab-level, and a different object
  from the board.
* **Further pattern generators**: differential-pair via transitions with return
  vias, crystal routing, antenna feeds. The fanout generator establishes the
  architecture; these are the obvious next tenants of it.

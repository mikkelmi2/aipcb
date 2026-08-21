# 0010 — High-speed capability: impedance, card edges, transitions and verification

* **Status:** Accepted, implemented in M11
* **Date:** 2026-08-21
* **Context:** milestone M11
  ([`docs/milestones/m11-highspeed.md`](../milestones/m11-highspeed.md)), building
  on [ADR 0006](0006-routing-approach.md) (the stretcher),
  [ADR 0007](0007-multilayer.md) (pairs, via columns and the stackup),
  [ADR 0008](0008-mech-placement.md) (the pattern-generator regime) and
  [ADR 0009](0009-pours.md) (filled zones, which the reference-plane check reads)

M11's question was not "can this tool route fast signals" — the router had been
laying coupled pairs since M8. It was **what a board is allowed to claim about
them**, and what has to be measured before it may. Everything below follows from
that: a width nobody typed, an outline with one author, a transition built rather
than refused, a corridor with room in it, and a report that says what it did not
check as clearly as what it did.

---

## The reference stackup, and the geometry it implies

The milestone's step 1: choose a four-layer stackup and compute the trace
geometry for 85 Ω and 100 Ω differential on it.

| Layer | Type | Thickness | εr |
|---|---|---|---|
| `F.Cu` | copper, 1 oz | 0.0350 mm | |
| prepreg, 7628 | dielectric | **0.2104 mm** | **4.4** |
| `In1.Cu` | copper, 0.5 oz | 0.0152 mm | |
| core, FR-4 | dielectric | 1.0650 mm | 4.6 |
| `In2.Cu` | copper, 0.5 oz | 0.0152 mm | |
| prepreg, 7628 | dielectric | 0.2104 mm | 4.4 |
| `B.Cu` | copper, 1 oz | 0.0350 mm | |

Copper 0.1004 mm, dielectric 1.4858 mm, solder mask 0.02 mm: **1.606 mm**, which
is the 1.6 mm the source declares. Signal / ground / power / signal, as the
milestone asks. It is a stackup a fabricator actually offers, which is the point:
an impedance derived against a laminate nobody stocks is an impedance for a board
nobody will build.

**The formulas.** IPC-2141's surface microstrip, with the standard coupling
factor:

```
Z0    = 87 / sqrt(er + 1.41) * ln(5.98 h / (0.8 w + t))
Zdiff = 2 * Z0 * (1 - 0.48 * exp(-0.96 s / h))
```

Solved for `w` by bisection, with the gap `s` as an *input* rather than an
output — a gap is a manufacturing choice and solving for both at once has no
unique answer. On the outer layers, against the 0.2104 mm prepreg:

| Target | Gap | Trace width | Comes out at |
|---|---|---|---|
| 85 Ω (PCIe Gen3) | 0.20 mm | **0.322 mm** | 85.0 Ω |
| 85 Ω | 0.15 mm | **0.289 mm** | 85.0 Ω |
| 100 Ω (SATA III) | 0.20 mm | **0.239 mm** | 100.0 Ω |

The reference board uses the 0.15 mm gap for its PCIe pairs and the 0.2 mm gap
for its SATA ones, and the reason is mechanical rather than electrical: a pair
that starts between two pads 0.5 mm apart cannot be wider than the pads it starts
from, and 0.289 + 0.15 = 0.439 mm fits where 0.322 + 0.2 = 0.522 mm does not.

### Finding 1 — the two formulas in this codebase disagree by 8 %

M8 estimated a gap from an `impedance_ohm` target using Hammerstad, which ignores
copper thickness. IPC-2141 does not. Measured on the stackup above, a 0.322 mm
trace over 0.2104 mm of prepreg at a 0.2 mm gap:

| Formula | Z0 | Zdiff |
|---|---|---|
| IPC-2141 | 52.6 Ω | **85.3 Ω** |
| Hammerstad, as implemented here | 57.2 Ω | **92.7 Ω** |

Eight percent is more than the tolerance any of this is claimed to, so which one
is used has to be a decision rather than an accident. **The controlled-impedance
path uses IPC-2141 throughout — the derivation *and* the audit that later checks
it.** An audit that recomputes the target with a different formula is auditing the
arithmetic rather than the board. `estimate_gap` keeps Hammerstad, so that no
board built before M11 changes its geometry; both live in `aipcb/impedance.py`
with the disagreement written above them.

### Finding 2 — `layout.stackup.layers` was decorative, and had to stop being

The block has been in the schema since M1 and nothing derived geometry from it:
`dielectric_thickness_mm` divided the leftover board thickness evenly between the
copper layers. On this stackup that gives **0.48 mm** where the prepreg under
`F.Cu` is **0.2104 mm** — a factor of 2.3, which is a 40 % error in the derived
width. A declared stack is now honoured, and only when it is *complete*: its
copper entries must be exactly the board's copper layers in order, or it is
ignored. A partial declaration is worse than none, because it looks authoritative.

---

## The M8c refusals, and which of them M11 had to eliminate

The milestone's step 2. `route/pairs.py` refuses a pair in seven places. Sorted by
what M11 does about them:

| Refusal | M11's answer |
|---|---|
| the centre-line will not tighten | **still legitimate.** M11d rule 1 makes it *more* likely, on purpose, and the hint now names `standoff_k` when the standoff is the probable cause. |
| the two ends would cross over | **still legitimate.** Planarity, as ADR 0007 records. |
| the fan-out will not reach the pads | **still legitimate.** |
| more than a third of each half is fan-out | **still legitimate**, and now joined by `max_uncoupled_mm`, which is a length rather than a fraction and is a *refusal* rather than a fallback. |
| the halves come closer than the pair's pitch | **still legitimate.** |
| the fan-out collides with something | **still legitimate.** |
| **the pair would have to change layer** | **eliminated.** This is M11c. ADR 0007 declined to build it and gave the arithmetic as the reason; M11c does the arithmetic instead of stopping at it. |

---

## Decision 1 — a net class states an impedance, and the width follows

`impedance_diff_ohm` on a class derives the pair's width from the declared
stackup. An explicit `diff_pair_width_mm` still wins — the board is built from
what the source says, never from what the target implies — and `aipcb validate`
reports the disagreement when it exceeds 10 %, with both numbers and the
impedance the override will actually produce.

The reference plane is *declared*, not inferred: `reference: In1.Cu` on the class.
It is consumed by the M11e checks and never by the router, exactly as the
milestone asks. Validation checks that the layer exists, that it is not the layer
the class routes on, that it is declared a plane, and that something pours copper
onto it — because **a reserved layer with no copper on it is not a reference**,
and before M10 there was no way to tell the difference.

### The naming deviation, stated plainly

The milestone's example writes `impedance_diff: 85`, `max_skew: 0.125` and
`max_uncoupled: 2.0`. This format spells a unit into every field that has one —
`trace_width_mm`, `clearance_mm`, `impedance_ohm`, `max_skew_mm` — and the
fields are called `impedance_diff_ohm`, `max_skew_mm` (which already existed and
is reused) and `max_uncoupled_mm`. The milestone's `class: diff_pair` inside a
net class is not implemented at all: in this format the class *name* is the key
and pair-ness comes from `diff_pair:` on the nets, so a second way of saying it
would be a second thing to keep in agreement.

---

## Decision 2 — a card-edge footprint's `Edge.Cuts` is a specification, not a contribution

`Connector_PCBEdge:BUS_PCIexpress_x1` draws eleven `Edge.Cuts` primitives of its
own: the tongue's two sides, the chamfered leading edge, and the keying notch with
its rounded end. In KiCad's own workflow those *are* the board outline there, and
the designer draws the rest of the edge to meet them.

aipcb cannot work that way, for two reasons that are not matters of taste:

* **the router would not see it.** Free space comes from the board's top-level
  `Edge.Cuts` graphics; geometry inside a footprint is invisible to it, so the
  router would think the tongue was not board and refuse to route to the fingers;
* **`board:` is the frame.** The pour scope, the placement checks, the coordinate
  conversion and the edge-clearance inset all come from the `board:` block. An
  outline that is partly somewhere else is an outline those five things disagree
  about.

**So the footprint's `Edge.Cuts` is stripped when the board is written, and
`checks/edge.py` verifies that the `board:` block reproduces it to within
0.01 mm.** Where it does not, the error hands the missing vertices back in the
source's own frame and syntax, so the fix is a paste rather than a calculation.

### Finding 3 — emitting both authors produces a board KiCad rejects

Measured rather than assumed. With the footprint's geometry left in *and* the
outline drawn, `kicad-cli pcb drc` reports:

```
error invalid_outline: Board has malformed outline (self-intersecting)
   [board outline edge segment 14, footprint J1]
```

twice, plus ten `copper_edge_clearance` errors that are the same duplication seen
from the other side. Stripping the footprint's copy removes all twelve.

### The cost, and it is a real one

KiCad's `lib_footprint_mismatch` rule notices that the placed footprint is no
longer byte-identical to its library copy, and reports it as a warning. It is
right. There is no way to have one outline author *and* an unmodified footprint,
and of the two the outline matters more. The warning is recorded as a known issue
on `examples/pcie-sata` with this reason attached, in `tests/test_check_loop.py`
and `tests/test_board.py`.

### Finding 4 — the shared-pad-UUID defect does not bite the card edge

`docs/roadmap.md` records that pads sharing a *number* share a UUID, and M11's
brief warned that an edge connector was where that would bite hardest —
"edge connectors have many same-numbered/mirrored pads, exactly the M7 lesson".

Measured on the board M11b actually integrates: **it does not bite at all.**
KiCad's PCIe x1 footprint numbers all 36 contacts distinctly — `A1`..`A18` and
`B1`..`B18` — so every finger has an identity of its own in the file as well as in
the router. 36 pads, 36 distinct numbers, 36 distinct UUIDs.

The defect is still there, and the same board proves it: the JST connector each
SATA port uses has two shell tabs both numbered `MP`, they share one UUID, and the
router tells them apart anyway because `route.obstacles` keys on
`reference#index` (`J2.MP` and `J2.MP#2`). Both facts are asserted as tests, so
if the UUID scheme is ever fixed the test says so rather than passing quietly.

**The warning was worth heeding and the answer is empirical: it depends entirely
on the footprint.** `Connector_PCBEdge:BUS_PCI` has 240 pads over 120 numbers —
every contact drawn as a long finger and a short one — and an edge connector on
*that* footprint would be squarely in the defect's path.

---

## Decision 3 — the transition column opens out, and the splay is measured

ADR 0007 refused to let a pair change layer and gave the reason as arithmetic:
"a 0.6 mm via at a 0.54 mm pitch is one piece of copper". It is. Two 0.4 mm vias
at a 0.4388 mm pitch leave **0.039 mm** of laminate between two nets that want
0.15 mm.

M11c does the arithmetic rather than stopping at it. The signal-via column opens
out to `max(pair pitch, via diameter + class clearance)` — 0.55 mm on the
reference board — the halves splay to reach it and close again on the far side,
and **that splay is uncoupled length, counted against `max_uncoupled_mm` like any
other.** The discontinuity ADR 0007 named is still there; the difference is that
it is now built deliberately and measured, rather than avoided by refusing the
pair.

The return vias go on the line the two signal vias sit on and nowhere else: the
pair arrives along the travel axis on one layer and leaves along it on the other,
so every other direction is in the pair's way. How far out they sit is not "as
close as looks right" either — it is the room the pair's own corridor needs,
standoff included, and the router's inflation over-statement allowed for.

### Finding 5 — a return via placed by eye costs the pair its coupling

Measured while building the reference board. Placed at 0.925 mm from the
transition centre, against the 1.007 mm the pair's corridor needs on that
geometry, the receive pair could not tighten, fell back to two separately routed
nets, and those two took detours that then cost eight further pairs their
coupling. **Eighty-two microns, and 9 of 14 coupled runs.** The generator now
computes the distance instead of choosing it.

### Finding 6 — KiCad normalises a through via's recorded span

A via written `(layers "F.Cu" "In2.Cu")` with a through drill comes back from
`pcbnew` written `(layers "F.Cu" "B.Cu")`. That is not wrong — a through via *is*
drilled the whole way — but it erases the one thing a stub calculation needs,
which is where the signal stopped. Every stub measured off the filled board
therefore came out at exactly zero: comfortable, and false. **The stub is taken
from the router's own record of what it drilled**, and the filled board is used
only for what it is authoritative about, which is copper.

---

## Decision 4 — three bounded stretcher rules, and no more

M11d's scope was fixed by its specification and this ADR does not widen it. What
is worth recording is what the three rules turned out to *cost*, because two of
the three are less free than they look.

**Rule 1, the standoff corridor.** Tightening against `clearance x k` rather than
the bare minimum. The default is 3, and on the reference board 3 is not
available: a pair leaving a 0.5 mm pad pitch has 0.625 mm to its neighbour and a
0.73 mm corridor of its own, so 0.45 mm of standoff does not exist there. **At
k = 3 every pair on that board is refused** — correctly and loudly, with a hint
naming `standoff_k`. The board sets 1.4, which is what the package will give, and
the arithmetic is written into the source rather than defaulted, so the number is
somebody's decision. Two further consequences, both real:

* the standoff applies to the *centre-line* only. The fan-out at each end is not
  coupled to anything, and asking it to stand off as well is what makes a pair
  leaving a fine-pitch package untightenable rather than merely tight;
* it scales `RouteRules.corridor`, which is the A\* congestion penalty's input.
  A standoff makes the search more gate-averse as well as the geometry roomier.

**Rule 2, no wall-hugging.** "Runs *parallel* to another copper feature" is the
load-bearing word, and the first implementation dropped it: measuring the length
of centre-line inside a feature's skirt counts going past the end of a pad as
hugging it. Measured on the reference board, that reported the QFN escape as
1.11 mm of hugging where the parallel part is the pad's own 0.875 mm — under the
1.0 mm threshold. The stretch is now counted only while the distance holds to
within one gap of its closest approach, which is what "parallel" means.

**Rule 3, the coupling budget.** Measured *after* length matching, because
meanders are added into the fan-out legs and a budget checked before them is a
budget checked against the wrong number. Over budget the pair is refused by
raising rather than returning, so the caller hands it over via M9f instead of
quietly routing the halves separately — which is the difference between "this
pair is not built" and "this pair is built worse than you asked for".

### Finding 7 — an `Edge.Cuts` arc costs the router its clearance

Not M11's, but M11's board is the first to hug one. Arcs are approximated by 24
chords per circle, and a chord runs *inside* its arc by the sagitta — 8.13 µm on
the 0.95 mm keying notch. Where the board is on the *outside* of the arc, as it
is at a notch, the free area is larger than the board and the router spends the
difference. KiCad reported ten `copper_edge_clearance` errors at 0.1419 mm
against the 0.1500 mm asked for; 0.1500 − 0.1419 = 0.0081 mm, which is the
sagitta to two significant figures. `RoutingEnvironment.arc_slack` adds the worst
sagitta on the board to the edge keep-out, which costs microns of routable area
and makes the approximation safe in both directions.

### Finding 8 — a fabricator needs web between holes, not daylight

Also not M11's, and also first seen here. `merge_overlapping_holes` merged same-net
vias whose holes *overlapped*. Two GND vias landed 0.3250 mm apart — not
overlapping, and 0.1255 mm of web against the 0.2495 mm minimum, which DRC
reported. The test is now the drill plus the minimum web.

---

## Decision 5 — the verification report says what it is not

M11e is rule-based geometry, and the module says so in its first line, the
report's JSON says so in a `method` field, and the README says so where a reader
will meet it. This is not modesty: a report that reads like a simulation and is
not one is worse than no report, because somebody will sign off against it.

What it does check is chosen so that every finding is a *measurement with a
position*: reference continuity by projecting each track onto its declared plane
every 50 µm and reporting each void or net change with its length and where it
starts; the width and gap read back off the copper rather than taken from the
intent; skew after meanders; stubs from the stackup; uncoupled length against the
budget. Severity is warning by default because these are engineering-judgement
items, with `verify: error` on a class for a project that wants them to fail the
build.

One exclusion is deliberate and worth naming: **a void within one via diameter of
one of the net's own vias is not reported.** A track that ends at a via crosses
that via's antipad by construction, and reporting it would be reporting the
transition against itself. What the plane loses there is the transition's own
doing, and the return vias beside it — counted and reported by the generator —
are the mitigation. A break anywhere else is exactly what the check is for.

---

## Consequences

* A design that declares no `impedance_diff_ohm` behaves exactly as it did before
  M11. `standoff` is 1.0, rules 2 and 3 do not run, and the M11e report is empty.
  All ten pre-M11 examples rebuild byte-identically.
* `layout.stackup.layers` is now load-bearing where it is complete. No bundled
  example declared one before M11, so nothing moved.
* A board with a card-edge connector carries one KiCad DRC warning that cannot be
  removed without giving the outline two authors.
* The reference-plane check needs a filled board, and therefore needs ADR 0009's
  `pcbnew` subprocess. A design with no `pours:` gets `hs-reference-unchecked`
  rather than a silent pass.

## What was not built

Recorded in [`docs/roadmap.md`](../roadmap.md):

* **electromagnetic simulation.** M11e is rule-based by design; exporting geometry
  for an external solver is M12's subject and ADR 0011's.
* **full environment-controlled tightening** beyond the three M11d rules.
* **back-drilling and blind/buried optimisation** for stub elimination. The stub
  is measured and reported; removing it is a fabrication process this toolchain
  does not ask for.
* **placing the AC-coupling capacitors.** The check validates and measures their
  placement; it does not move them. The placer is M9's, and a routing-side
  generator that moved parts would be a larger change than M11 sanctions.

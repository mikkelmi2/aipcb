# The long tour

This was the README until M15, when the front page was cut down to a shop window.
Nothing here was thrown away: this is the full walk through what the tool does,
command by command, with the output it actually produces.

It overlaps [`format.md`](format.md) (which is the *reference* for every key in a
design file) and [`workflows.md`](workflows.md) (which is the *task*-shaped guide).
Read this one if you want a single continuous read-through.

---

## Why

The primary author of this format is an AI agent working in a loop: edit source,
compile, run checks, read structured feedback, fix. That loop needs things a
schematic editor's native format cannot offer.

**Intent is stored, not inferred.** A capacitor is not just a capacitor; it is
`role: decoupling, for: U1` with a `reason:` attached. Nothing has to guess why it
is there — not the placement engine, not the reviewer, not the agent that picks the
design up six months later.

**Feedback is structured and source-referenced.** Every problem comes back as a
diagnostic naming a file, a line, a stable error code, and where possible a fix:

```
examples/usb-port/design.yaml:53:3: error[asymmetric-diff-pair]: net 'USB_DP'
names 'USB_DM' as its differential partner, but 'USB_DM' names USB_DPP
  at: nets.USB_DM.diff_pair
  hint: set `diff_pair: USB_DP` on 'USB_DM'
```

**Output is deterministic.** Every element's UUID is a hash of its path in the
source, never of generation order or time. The same source produces byte-identical
files, so `git diff` shows only what actually changed — and a violation reported by
`kicad-cli` against a UUID maps straight back to the line that owns it. The
guarantee is about **build output**: copper pours are emitted unfilled, and the
fill is a derived artefact regenerated at check and export time by KiCad's own
engine ([the stability policy](format.md#the-stability-policy)).

**Reads can be partial.** `aipcb query` extracts one module, one net class, or a
one-line status per block, so an agent can look at the part of a design it is
working on without paying for the whole thing in context.

**What is checked is separated from what is not.** A net class can state an
impedance and let the stackup derive the trace geometry, and `aipcb check` will
project every controlled-impedance track onto the plane its class declares and
report where the plane under it stops or changes net. That is **rule-based
geometry, not electromagnetic simulation**: it says the return path has somewhere
to go, not that it goes there. A gigabit board that passes every one of those
checks still wants a human and an SI tool before anybody signs it off, and the
report says so in its own output rather than leaving it to be assumed.

## The layer model

| Layer | What it holds | Where it lives |
|---|---|---|
| 1. Semantic schematic | components, nets, intent, hierarchical parameterised modules | `design.yaml`, [`aipcb.model.design`](../src/aipcb/model/design.py) |
| 2. Layout intent | stackup, placement rules, per-class routing rules, routing sketches | `layout:`, [`aipcb.model.layout`](../src/aipcb/model/layout.py) |
| 2. Mechanical | board outline, cutouts, edge clearance, fixed placement, fanout | `board:`, `placement:`, `fanout:`, [`aipcb.model.board`](../src/aipcb/model/board.py) |
| 3. Compiler / sync | elaboration, KiCad emission, check-loop feedback | [`aipcb.elaborate`](../src/aipcb/elaborate.py), `aipcb.kicad` |
| 4. Component database | parts, pinouts, limits, KiCad symbol/footprint bindings | `library/*.yaml`, [`aipcb.model.parts`](../src/aipcb/model/parts.py) |

No layer stores what a lower one can derive. Layer 1 has no coordinates; layer 2
has intent, not geometry.

## Quickstart

Requires **Python 3.12+** and **KiCad 9**. KiCad is the backend, not an optional
extra: compiling a design reads its stock symbol and footprint libraries, and
checking, rendering and exporting shell out to `kicad-cli`. Only `validate`,
`summary`, `query` and `schema` run without it.

> This page said "KiCad 8 or 9 is needed for the check and export commands;
> everything else runs without it" until M15, when running the quickstart in a
> clean container showed that `aipcb build` needs the libraries too. The Python
> floor moved to 3.12 in the same milestone, for a measured reason —
> [ADR 0013](decisions/0013-ci.md) Finding 4.

```bash
git clone <this repo> && cd aipcb
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/aipcb version
```

Validate one of the bundled examples:

```bash
.venv/bin/aipcb validate examples/usb-port/design.yaml
```

```
ok: no problems found
usb-port rev A: 7 components, 7 nets, 20 connections
```

Now break it on purpose — change `diff_pair: USB_DP` to `diff_pair: USB_DPP` under
`nets: USB_DM:` — and run it again. You get a located, coded diagnostic rather than
a stack trace. Add `--json` and you get the same thing in machine-readable form,
which is what the agent loop consumes:

```bash
.venv/bin/aipcb validate examples/usb-port/design.yaml --json
```

### Compiling

```bash
.venv/bin/aipcb build examples/usb-port/design.yaml --out build/
kicad-cli sch erc --severity-all build/usb-port.kicad_sch
kicad-cli pcb drc --severity-all --schematic-parity build/usb-port.kicad_pcb
```

```
Found 0 violations
Found 18 unconnected items
Found 0 schematic parity issues
```

The build writes a `.kicad_sch`, a `.kicad_pcb`, a `.kicad_pro` carrying the net
classes as KiCad design rules, and project-local `sym-lib-table` / `fp-lib-table`
files. Open either file in KiCad and it renders, checks, and plots like any other.

The unconnected items are the ratlines: `aipcb build` stops at footprints and an
outline, and `aipcb route all` is what turns those into copper. Everything else is
clean — in particular *schematic parity*, which is KiCad checking that every
footprint is tied to its symbol and every pad sits on the net the source says it
does.

Footprint placement comes from the design's intent rather than from a layout file.
Components joined by a `group` or `max_distance` constraint, or by a `for:`
reference, are clustered and packed together; the clusters are then shelf-packed
into the board outline. If they do not fit, the diagnostic says what size would:

```
warning[placement-overflow]: the components do not fit inside the board outline:
they need about 33.3 x 35.6 mm, and the outline gives 22.0 x 16.0 mm
```

Connectivity is expressed with net labels on pin stubs rather than routed wires,
and symbols are laid out on a grid grouped by module instance. That is deliberate:
placement is naive, connectivity is exact. The test suite does not take ERC's word
for it either — it exports KiCad's own netlist and asserts it matches the source
net for net, pin for pin.

Rebuild without changing anything and the output is byte-identical, so the file is
not even rewritten.

### The check loop

`aipcb check` builds the design, routes it, runs KiCad's ERC and DRC, and reports
what they found *against the source*:

```bash
.venv/bin/aipcb check examples/usb-port/design.yaml
```

Set an impossible clearance on the `usb` net class and KiCad's complaint comes back
pointing at the line that caused it:

```
examples/usb-port/design.yaml:69:3: error[kicad-clearance]: Clearance violation
(netclass 'usb' clearance 2,5000 mm; actual 0,2500 mm) [pad J1 pad 1, pad J1 pad 2]
  at: components.J1.pins.1
  hint: widen the clearance for this net class under `net_classes:`, or move the
        parts apart with a `keep_apart` constraint
```

The mapping is exact, not heuristic. Every item in a KiCad report carries a UUID,
and every UUID we emit is a hash of a source path, so recovering the owner is a
dictionary lookup — no matching on coordinates, no parsing of description strings.
A test asserts that *every* UUID appearing in a generated file maps back, so the
index cannot silently fall behind the emitters.

A UUID that does not map is reported too, and says what it means: the board
contains something `aipcb` did not generate, so a human added it in KiCad and that
is where the fix belongs. That case becomes load-bearing in M6.

Routing before checking is deliberate: a DRC pass over a board with no copper on it
has checked almost nothing. Connections the router will not deliver legally are
handed over rather than squeezed in, so `--json` carries both a DRC result that means
something and an explicit list of what is not routed. `--no-route` gets the old
behaviour where only the build matters.

### Your edits survive a rebuild

`aipcb build` is incremental. Open the generated board in KiCad, move a connector,
route a pair by hand, pour a zone — then rebuild:

```
info[preserved-edits]: kept manual work from the existing board: 7 hand-placed
footprints; 1 segment, 1 zone
  hint: run `aipcb build --fresh` to regenerate from source instead
```

The rule is one sentence: **the source owns what it declares; everything else
belongs to the person who drew it.** Every generated footprint records a
fingerprint of the source facts that determine it — its part, its connections, its
side, the constraints naming it. If that fingerprint still matches, your position
stands. If the source changed its mind, the source wins and says so:

```
info[placement-regenerated]: 1 footprint moved because the source changed: R1
```

Editing a `reason:` does not count as changing your mind, so improving a comment
never shoves a hand-placed part back onto the grid.

Copper belonging to a net the design no longer has *is* removed, with a warning —
an orphaned track is a short waiting to happen. And a board file that cannot be
parsed is never silently overwritten; it might be hours of somebody's routing.

### Fabrication output

```bash
.venv/bin/aipcb export examples/usb-port/design.yaml --out fab/
```

```
exported 16 files to fab/ (fill (1/1 zones), gerbers, drill, position, bom)
```

Gerbers for every copper and technical layer, Excellon drill files split by plating
with a drill map, a `.gbrjob`, a grouped BOM carrying the descriptions from your
component database, and a placement file. That is the whole path: source → fab data,
with no step that required opening a GUI.

Every coordinate in the package is measured from the board's own bottom-left corner
rather than from a corner of the A4 sheet KiCad happened to put it on. That sounds
like a detail and is not: consumers that parse unsigned coordinates *silently drop*
what lands outside their frame, so a package in page coordinates is not rejected, it
is quietly misread. Two of aipcb's own exports had that shape until M12 read the
files back.

### Simulating the pairs

Rule-based checks say a differential pair *should* be 100 Ω. A field solver says what
it is.

```bash
.venv/bin/aipcb simulate examples/pcie-sata/design.yaml
```

```
simulating pcie-sata into examples/pcie-sata/out/si
  PCIE_TXN+PCIE_TXP        simulated   128.2 s  Zdiff   80.0 ohm  (target 85, -5.8%)
  PCIE_RXN+PCIE_RXP        simulated   220.9 s  Zdiff   72.8 ohm  (target 85, -14.4%)
  REFCLKN+REFCLKP          simulated   473.6 s  Zdiff   49.8 ohm  (target 85, -41.4%)
  ...
```

Those deviations used to be much larger — 66.6, 61.5 and 50.9 ohm on the same three
links — and they were a finding about the *width solver*, not about the router. Both
closed forms aipcb derived a width from modelled a **bare** microstrip, and every one
of these pairs has a ground pour within a class clearance of it. M13b gave the
derivation a coplanar model, which reads the `pours:` block, solves the pair against
the ground beside it as well as the plane under it, and says in `check`'s output
which of the two models each class used.

What did not come free is the mesh. A coplanar-derived pair is narrower, and the cell
size M12 calibrated on a wider one returns **12 ohm for an 85 ohm line** — converged,
exit code zero, no warning. The mesh now follows the geometry.
[ADR 0012](decisions/0012-coplanar-impedance.md) and
[`docs/reports/m13.md`](reports/m13.md) carry both measurements, and the honest
state of the third link: `REFCLK`'s slice traps energy and its number is not one to
act on.

Each pair is cut out of the routed board as a **slice** — a small self-contained
`.kicad_pcb` carrying that pair, the copper near it, the planes it is referenced to
and four simulation ports — and handed to openEMS through Antmicro's gerber2ems in a
pinned container. What comes back is S-parameters; what you get is the same
source-referenced findings as `check`, plus the raw Touchstone file if you would
rather work in scikit-rf.

Three things the source knows that a geometric slicer could not:

* a pair split by `role: ac_coupling` capacitors is **one link**, not two stubs, so
  the capacitors become copper and the ports go where the link really ends;
* the reference plane comes from `layout.stackup.planes`, not from guessing which
  copper is nearest;
* each port is terminated in half the class's declared `impedance_diff_ohm`, so the
  measurement is of the line rather than of the mismatch.

It is a separate command and stays one. A pair costs one to thirteen minutes where
the whole of `check` costs seconds — and, more to the point, an impedance eight
percent off target is a number to think about, not a correctness failure. Re-running
an unchanged pair costs nothing: the cache key is a hash of what the solver actually
reads.

The honest limit, in the same place as the feature: **this validates the layout, not
the board.** Every number comes from the stackup the source declares. A fabricator
who presses a different prepreg builds a different impedance and nothing here will
know. It finds a pair routed too narrow or a reference that is not where the class
said; it does not replace an impedance coupon.

Running it needs a container runtime and a locally built gerber2ems image — the only
part of aipcb that needs either. [ADR 0011](decisions/0011-si-simulation.md)
records the pinned commits and how the image was built.

### Routing

Routes are stored as **topology**, not geometry — which obstacles a wire passes and
on which side:

```yaml
layout:
  routes:
    - net: CROSS
      from: J1.1
      to: J2.1
      passes:
        - obstacle: U1.2
          side: left      # looking along the direction of travel
          reason: Going over the MCU leaves the lower half of the board for SENSE.
```

Nothing there is a coordinate, which is the point: move a part and the sketch is
still true, so only the tightening re-runs. Flip `side` to `right` and the same
sketch produces a different board.

```bash
.venv/bin/aipcb route check examples/routing-demo/design.yaml   # is it buildable?
.venv/bin/aipcb route all   examples/routing-demo/design.yaml   # build it
```

Geometry comes from rubber-band tightening: obstacles are inflated by the clearance
the net needs, the remaining free space is triangulated, the sketch picks a homotopy
class, and the funnel algorithm pulls the wire taut inside it. Because the obstacles
were inflated *first*, the shortest path through what is left is automatically a
legal one — clearance holds by construction rather than being checked and patched.

Connections without a sketch are routed automatically, across every signal layer
the stackup allows. Layer, via position and corridor come out of one search rather
than three sequential decisions, and the corridors are settled by **negotiation**:
every net is routed as though it had the board to itself, the corridors that end up
over-subscribed get more expensive, the nets that lost the argument are ripped up
and re-routed, and it repeats until every cut across the free space carries no more
copper than it has room for. Ripping up costs nothing, because a route at that stage
is a set of subscriptions rather than a piece of copper.

```bash
.venv/bin/aipcb route all examples/mcu-4layer/design.yaml
```

```
routed 36 connections (0 unrouted) on B.Cu, F.Cu, 140 track segments and 5 vias,
547.86 mm of copper
```

The source gets a say in the outcome. A net class can state a `priority` and a
`rip_up` policy, and on each contested corridor the net that is hardest to rip up
keeps its place while the rest go round:

```yaml
net_classes:
  clk_sys:
    priority: 90
    rip_up: protected
```

The stackup gets a say too. A layer listed under `stackup.planes` is closed to
signal routing — enforced, not advisory — and a via is modelled as a *column*, so it
blocks every layer its barrel passes through and not only the two it connects. What
each of those choices costs is one file: [`docs/routing-costs.md`](routing-costs.md).

Every routed board in `examples/` passes `kicad-cli pcb drc`, and **all seven route
completely** — including `led-blinker` and `usb-port`, which cannot be routed on one
layer at all: a DIP-8's pin rows and a Micro-B receptacle's 0.65 mm pitch leave no
corridor at 0.25 mm tracks and 0.2 mm clearance. The second layer is the answer, and
the router finds it. Where a connection genuinely cannot be made, it is reported with
a reason rather than silently skipped.

A differential pair is tightened *once*, as a single centre-line wide enough for
both traces and the gap, then offset to either side — so the gap is right
everywhere by construction and the halves come out the same length. Where they do
not, the shorter half is meandered in its own corridor until they do:

```
info[diff-pair-length-matched]: USB_DM+USB_DP: 1.591 mm of meander added to USB_DM
to meet its 0.150 mm skew budget
```

Where a pair cannot honestly be coupled, it says so and routes the halves separately
rather than shipping something that only looks like a pair:

```
warning[diff-pair-not-coupled]: DEV_DM+DEV_DP could not be fanned out to its pads:
pair fan-out DEV_DP: the end is not in the routable area
```

The algorithm, the prior work behind it, and the several ways it can be got subtly
wrong are in [`docs/topology.md`](topology.md),
[ADR 0006](decisions/0006-routing-approach.md) (one layer) and
[ADR 0007](decisions/0007-multilayer.md) (all of them).

### The board is a shape, not a rectangle

Real boards go inside things. `board:` is where the mechanical boundary lives — an
outline that may have arcs, cutouts that pierce every layer, and the edge clearance
copper must keep:

```yaml
board:
  origin: bottom_left          # the source frame is Y up
  outline:
    polygon:
      - [0, 0]
      - [42, 0]
      - { arc_to: [48, 6], center: [42, 6] }
      - [48, 26]
      - [36, 26]
      - [36, 34]
      - [0, 34]
  cutouts:
    - rect: [[16, 10], [22, 18]]
      reason: The display's flex tail passes through here.
  edge_clearance: 0.3
```

That is not just an `Edge.Cuts` export. The placer packs into the real polygon, so
nothing lands in the missing corner or over the hole; the validator checks
courtyards against it; and the router sees the outline and every cutout as
obstacles in each layer's triangulation, so two points either side of a slot are
genuinely in different homotopy classes.

`placement:` says where the enclosure puts a part, in three levels:

```yaml
placement:
  J1:
    fixed: { x: 4.0, y: 17.0, rot: 270 }
    reason: The receptacle lines up with the port in the enclosure's west wall.
  H1:
    fixed: { x: 4.0, y: 28.5 }
    role: mounting_hole
  SW1:
    edge: { side: north, offset_range: [12, 30] }
    reason: Under the moulded cap in the lid.
  D1:
    region: { rect: [[28, 27], [34, 32]] }
    reason: Under the light pipe.
```

`fixed` outranks everything: a group that names a fixed part deforms around it, and
the anchor does not move. Conflicts are caught by `aipcb validate` against geometry
alone — two fixed courtyards overlapping, a part off the polygon, a courtyard over a
cutout, an `edge` span with nowhere to go, a `max_distance` the anchors already make
impossible.

If somebody nudges a fixed part in KiCad, `aipcb build` puts it back and says so.
When the board is right and the source is stale, `aipcb sync-placement` goes the
other way and rewrites the YAML in place, comments and all.

[`examples/enclosure`](../examples/enclosure/design.yaml) is the worked example: a
shaped board with an arc and a cutout, a fixed connector and two fixed mounting
holes, an edge-constrained button and a region-constrained LED. It builds, places,
routes completely, and passes KiCad's DRC with zero violations.

### Escaping a fine-pitch package

A QFN-32 on a 0.5 mm pitch leaves nothing to route out through. That is not a
routing problem, it is a pattern, and `fanout:` asks for it:

```yaml
fanout:
  U1:
    style: auto
    escape_layers: [B.Cu]
    via: { drill: 0.2, diameter: 0.4 }
```

A deterministic generator runs *before* routing, lays a stub from each pad to a via
just clear of the part — staggered into two rows, because a single row at pad pitch
would be one piece of copper — and publishes those vias as the terminals the router
sees in place of the package's pads. The rubber-band router that follows has no idea
a fanout happened.

[`examples/qfn-fanout`](../examples/qfn-fanout/design.yaml) routes an ATmega328P in a
32-pin QFN to completion, with zero DRC violations.

### Copper pours, and who fills them

Nearly every real board wants ground on its outer layers, and many want a plane
split between two supplies. `pours:` says which net owns which copper:

```yaml
pours:
  - net: VCC
    layer: In2.Cu
    scope: board
  - net: GND
    layer: In2.Cu        # a split plane: ground takes this corner back
    region: { rect: [[42, 2], [54, 16]] }
    priority: 1          # higher priority keeps the copper where zones overlap

stitching:
  - net: GND
    between: [F.Cu, B.Cu]
    pattern: grid        # grid | edge | ring
    pitch: 5.0
```

`aipcb` emits the zone — its boundary and its rules — and **never a filled
polygon**. KiCad's own filler produces the copper, because KiCad's fill is what DRC
checks against; a second implementation would differ, and the difference would be a
bug on every board ([ADR 0009](decisions/0009-pours.md)). `aipcb build` stays a
byte-stable pure function of the source with its zones unfilled; `aipcb check` and
`aipcb export` fill a staged copy first, because KiCad plots whatever fill data is
in the file and an unfilled pour exports as *no copper at all*, silently.

Stitching vias are generated the same way fanout escapes are: a deterministic
pattern, laid after routing and before the fill, dropping positions that would foul
a track, a pad, another hole or the board edge — silently, but with the counts in
the report.

After the fill, `aipcb check` reads the filled polygons back and says what the
copper actually came out as:

```
examples/qfn-fanout/design.yaml:243:5: warning[plane-fragmented]: the GND pour on
B.Cu is in 5 pieces; its largest holds 86.2% of the copper, below the 90.0% this
pour asks for
  hint: a track crossing the plane is the usual cause; move it, give it a layer of
  its own, or stitch the pieces together
```

That is feedback, not a gate — fragmented-but-functional is common, and the warning
only fires when the source sets `min_contiguous:`.

Filling needs KiCad's Python module (the `kicad` package, not only `kicad-cli`), and
only for designs that declare `pours:`.

### When the board cannot be routed

The router does not deliver marginal geometry. When a connection cannot be made
DRC-clean, it is *handed over*: everything else is routed, and the refusal comes back
machine-readable.

```console
$ aipcb route all examples/overconstrained/design.yaml --json | jq '.routing.handed_over[0]'
{
  "net": "SWAP_B",
  "from": "J1.2",
  "to": "J2.3",
  "unrouted": "over_complexity",
  "reason": "the free area is split in two by other parts' clearances",
  "blocked_at": [
    { "layer": "F.Cu", "at": [108.075, 110.0], "width_mm": 4.15,
      "demand_mm": 1.8, "over_subscribed": false,
      "nets": ["SWAP_A", "SWAP_B", "SWAP_C", "SWAP_D"] }
  ]
}
```

An agent can act on that — add a layer, move a part, drop a priority. A human can
route those nets by hand in KiCad, and the incremental build then preserves that
copper and treats it as law. What the toolchain will never do is ship a board with
DRC violations and call it routed.

### Reading part of a design

An agent working on one corner of a board should not have to hold the whole thing
in context. `aipcb summary` is meant to be the first thing read about an unfamiliar
design, and small enough that reading it is never the expensive choice:

```bash
.venv/bin/aipcb summary examples/ldo-supply/design.yaml
```

```
ldo-supply rev A
  Unregulated DC in, 3.3 V out, using an AMS1117 LDO.
  7 components, 3 nets, 15 connections, 2 constraints
  net classes: ground x1, power x2

blocks:
  (top level)   2 parts  J1, J2  [connector]
  rail_3v3      5 parts  C1, C2, C3, C4, U1  [bulk, bypass, regulator]

constraints:
  max_distance: rail_3v3.CIN, rail_3v3.U
    Input capacitor loop inductance sets how well the LDO rejects supply transients.
```

`aipcb query module` then zooms in on one block — including the parts on the *other*
side of its ports, since a module read in isolation says nothing about what it
drives:

```
module rail_3v3
  components:
    C1    C_100N_0603  role=bypass  for=U
    ...
  ports (nets crossing the boundary):
    VIN [power]  inside C3.1, U1.3  ->  J1.1
    VOUT [power]  inside C1.1, C2.1, C4.1, U1.2  ->  J2.1
  neighbours:
    J1    CONN_PWR_1X02  role=connector
    J2    CONN_PWR_1X02  role=connector
```

`query component`, `query net`, `query net-class` and `query role` narrow further.
Every one of them takes `--json`, and text and JSON are two renderings of one
structure, so they cannot disagree about what a design contains.

### A minimal design

```yaml
name: divider
libraries: [../library/passives.yaml]

nets:
  VIN:  { class: power, voltage: 3.3 }
  MID:  { class: analog }
  GND:  { class: ground }

components:
  R1:
    part: R_10K_0603
    role: divider
    reason: Upper leg. Ratio sets the ADC's full-scale point.
    pins: { "1": VIN, "2": MID }
  R2:
    part: R_10K_0603
    role: divider
    for: R1
    pins: { "1": MID, "2": GND }
```

The three bundled examples build up from there:

* [`examples/led-blinker`](../examples/led-blinker/design.yaml) — an ATtiny85, an ISP
  header, and a parameterised `indicator` module.
* [`examples/ldo-supply`](../examples/ldo-supply/design.yaml) — a `regulated_rail`
  module instantiated like a function, with `count:` stamping out bypass caps.
* [`examples/usb-port`](../examples/usb-port/design.yaml) — a differential pair with
  an impedance target, skew budget, and routing intent.
* [`examples/routing-demo`](../examples/routing-demo/design.yaml) — exists for the
  routing: a signal crossing the board past an MCU that is squarely in the way.
* [`examples/diff-pair`](../examples/diff-pair/design.yaml) — a 100 Ω pair carried
  across a board, routed as a coupled pair with zero skew.
* [`examples/mcu-4layer`](../examples/mcu-4layer/design.yaml) — signal / ground /
  power / signal, with a DIP-8 the ISP bus has to get *under* and two planes the
  router will not put a signal on.
* [`examples/congestion`](../examples/congestion/design.yaml) — four wires reversed
  across a channel. Not routable on one layer; routable on two, and the negotiation
  works out which ones dive.

## Routing is optional

You do not have to trust an unknown autorouter to get value out of this. Routing is
one step of the pipeline, it is declared in the source, and there are three ways to
run it. Everything else — validation, placement, pours, ERC, DRC, the high-speed
checks, Gerbers — works identically in all three.

### Full auto

```bash
aipcb route all design.yaml     # topological router, DRC-clean or handed over
aipcb check design.yaml
```

The router refuses to deliver marginal geometry. What it cannot make legally it
*hands over*, naming the connection, the corridor that ran out of room, and the nets
contesting it.

### Hybrid — you own the critical nets, aipcb routes the rest

This is the mode most people want first. Declare which nets are yours:

```yaml
net_classes:
  rf:
    trace_width_mm: 0.4
    routing: manual        # every net in this class is yours
nets:
  SENSE:
    class: analog
    routing: manual        # or just this one
  CLK_OUT:
    class: rf
    routing: auto          # ...and this one opts back out of its class
```

```bash
aipcb route all design.yaml     # your nets are left untouched and listed as pending
# ...draw them in KiCad...
aipcb check design.yaml         # checks all copper identically, whoever drew it
```

### Fully manual

```bash
aipcb build design.yaml         # footprints placed, no copper
# ...draw all of it in KiCad...
aipcb check design.yaml --no-route
```

Whichever mode, `aipcb check --json` puts every net in one of four states:

| State | Meaning |
|---|---|
| `manual-routed` | declared manual, and copper for it is on the board |
| `manual-pending` | declared manual, and **there is no copper yet** |
| `auto-routed` | aipcb's router laid it |
| `handed-over` | the router tried and refused, with the reason and the blocking corridor |

`manual-pending` is the one to watch: it is where a board sits between "these pairs
are mine" and "I have drawn them", and to anything counting unrouted connections it
looks exactly like a finished board. `aipcb check` warns about it by name.

Hand-drawn copper is preserved by every later build and routed *around*, never
through. The full loop is in [`docs/workflows.md`](workflows.md); routing
declared-manual nets with an external router such as Freerouting, headlessly, is in
[`docs/external-routers.md`](external-routers.md).

## Commands

| Command | What it does |
|---|---|
| `aipcb validate DESIGN` | schema and semantic checks, with source-referenced diagnostics |
| `aipcb build DESIGN` | compile to `.kicad_sch`, `.kicad_pcb`, `.kicad_pro` and the project library tables; `--render` also plots the schematic to `review/` |
| `aipcb check DESIGN` | build, route, run KiCad's ERC and DRC, report violations against the source |
| `aipcb sync-placement DESIGN` | report parts moved in KiCad, and write their positions back into the source |
| `aipcb export DESIGN` | Gerbers, drill files, BOM and placement file into `out/`; `--dsn` writes a Specctra DSN for an external router instead |
| `aipcb import DESIGN --ses FILE` | import an external router's session file, splice it in, verify it against the source, and check the result |
| `aipcb route check DESIGN` | verify route topologies are realizable, and that they fit alongside each other |
| `aipcb route all DESIGN` | route the board across every signal layer and write tracks and vias |
| `aipcb simulate DESIGN` | solve each differential pair with openEMS and report impedance, return and insertion loss |
| `aipcb summary DESIGN` | one-line-per-block overview |
| `aipcb query ...` | read one module, component, net, net class or role |
| `aipcb parts DESIGN` | list the parts the design's libraries provide |
| `aipcb schema` | emit the JSON Schema for the source format, for editor completion |
| `aipcb version` | report the aipcb and KiCad versions in use |

Every command takes `--json`. Exit codes are `0` clean, `1` errors found, `2` input
unreadable.

## Status

| Milestone | State |
|---|---|
| M1 — format, validation, examples | **done** |
| M2 — schematic compile (`.kicad_sch`, passes ERC) | **done** |
| M3 — netlist and board skeleton | **done** |
| M4 — check loop over `kicad-cli` ERC/DRC JSON | **done** |
| M5 — query layer for partial reads | **done** |
| M6 — incremental build preserving manual edits; Gerber export | **done** |
| M7a — topology model and validation | **done** |
| M7b — rubber-band stretcher, DRC-clean tracks | **done** |
| M7c — congestion-aware auto-topology | **done** |
| M7d — differential pairs, impedance and skew | **done** (meander length-matching landed with M8c) |
| M8a — layered triangulations, via columns, cut capacity | **done** |
| M8b — multilayer search with negotiated congestion, priority and rip-up | **done** |
| M8c — pairs as one object, meander length-matching, layer preference | **done for pairs and meanders**; crossovers are still refused with a reason ([ADR 0007](decisions/0007-multilayer.md)), and a pair *changing layer* is built since M11c |
| M8d — stretcher integration and acceptance | **done**: all seven examples route completely, and pass DRC with one warning on the *unfilled* board ([`diff-pair`](../examples/diff-pair/design.yaml) keeps a copper sliver where a ground track threads between a header's two pads; since M10 the ground pour fills the wedge and `aipcb check` reports zero) |
| M9-outline — the board boundary as a source object | **done**: arcs, cutouts and edge clearance, propagated into the placer, the validator and every layer's free space |
| M9a/M9b — three-level placement, fixed parts as anchors | **done** |
| M9c — mechanical conflict validation | **done**: eleven checks, all before anything is built |
| M9d — drift reporting and `aipcb sync-placement` | **done** |
| M9e — pattern-based fanout | **done**: perimeter and dog-bone escapes, thermal vias in an exposed pad; via-in-pad only when asked for |
| M9f — honest failure | **done**: what the router will not deliver legally is handed over, with the corridor that blocked it |
| M10a — pours as source intent | **done**: layers or region, priorities, thermal or solid, per-pad-instance overrides |
| M10b — fill integration and the stability policy | **done**: KiCad's own filler in a `pcbnew` subprocess with a version lock ([ADR 0009](decisions/0009-pours.md)); build stays byte-stable and unfilled |
| M10c — stitching vias | **done**: `grid`, `edge` and `ring` patterns, deterministic, with skip counts reported |
| M10d — plane-integrity report | **done**: islands, largest-island fraction and bounding boxes read back off the filled board |
| M11a — controlled-impedance net classes | **done**: `impedance_diff_ohm` and a declared stackup derive the pair's width; an explicit width that disagrees by more than 10% is reported with the impedance it will actually produce |
| M11b — card-edge connector integration | **done**: the footprint's own `Edge.Cuts` is a specification the `board:` block has to reproduce, checked to 0.01 mm, with the missing vertices handed back in the error; pour keepout, thickness check and the bevel fab note ([ADR 0010](decisions/0010-highspeed.md)) |
| M11c — pair via transitions and AC coupling | **done** for transitions: two signal vias at a column opened out from the pair's pitch, ground returns, the stub computed from the stackup, and two coupled segments for the router. **Partly** for AC coupling: the pairing, the symmetry and the budget are validated and measured; the capacitors are not *placed* |
| M11d — stretcher environment discipline | **done**: the three bounded rules, and no others. The default 3x standoff is not available on a 0.5 mm pad pitch and refuses loudly there |
| M11e — high-speed verification report | **done**: reference continuity projected onto the declared plane after the fill, geometry audit off the copper, skew, stubs and coupling budget. Rule-based, and it says so |
| M12a — slice generation | **done**: one slice per pair, cut out of the routed board with its neighbours, planes and vias; `role: ac_coupling` parts bridged so a split lane is one link; a straight launch and four ports at the ends. Slices are byte-stable |
| M12b — simulation orchestration | **done**: openEMS in the pinned container ADR 0011 records, sequential (openEMS already uses every core), cached on a hash of what the solver reads, per-pair failures, and a manifest. No `--parallel`, measured rather than assumed |
| M12c — structured results | **done**: differential impedance against the class target, worst return loss and its frequency, insertion loss at the class's key frequencies, group delay, and pass/warn per metric — as `check`-shaped findings pointing at the source. Raw S-parameters kept as Touchstone. The thresholds are engineering defaults and say so |
| M13 — routing correctness, impedance model, skew verdict | **done**: the obstacle-set merge that was losing copper, the coplanar model M12 measured, and the fitted-Δτ verdict ([`docs/reports/m13.md`](reports/m13.md)) |
| M14a/M14b — readable schematics | **done**: placement driven by roles, `for:` references, module structure and the signal-flow graph; power symbols, staggered stubs, module frames. Overlapping items across the eleven examples: 71 → 0; mean decoupling-to-IC distance on `pcie-sata` 157.5 mm → 27.8 mm; the sheet dropped from A1 to A3. Netlists byte-identical, ERC still 0 ([ADR 0003 amendment](decisions/0003-schematic-generation.md)) |
| M14c — manual-edit policy for schematics | **done**: M6's preserve never covered `.kicad_sch` and silently overwrote edits. The sheet is now explicitly a *view*, and a rebuild that discards an edit says so |
| M14d — `routing: manual` | **done**: declared on a class or a net, honoured by the router, reported as four distinct states ([`docs/workflows.md`](workflows.md)) |
| M14e — headless external-router bridge | **done**: `aipcb export --dsn` / `aipcb import --ses` through the `pcbnew` subprocess, existing copper fixed on the way out and spliced rather than replaced on the way back. Round-tripped through Freerouting 2.3.0 headlessly with 0 DRC errors ([`docs/external-routers.md`](external-routers.md)) |

## Documentation

* [`docs/format.md`](format.md) — the source-format reference.
* [`docs/topology.md`](topology.md) — how a route is stored, and how it becomes
  copper on however many layers it takes.
* [`docs/routing-costs.md`](routing-costs.md) — every number the router weighs,
  with its default and why.
* [`docs/workflows.md`](workflows.md) — working in KiCad alongside aipcb: what
  is preserved, moving parts, the three routing modes, and why the schematic is a
  view rather than a document.
* [`docs/external-routers.md`](external-routers.md) — the headless DSN/SES
  bridge: three commands, the contract, and the rule about controlled impedance.
* [`docs/roadmap.md`](roadmap.md) — what is deliberately not built, and why.
* [`docs/reports/`](reports/) — one delivery report per milestone, with the
  numbers each one was measured against.
* [`docs/decisions/`](decisions/) — architecture decision records. Start with
  [0001 (KiCad I/O)](decisions/0001-kicad-io.md), which explains why this
  project writes its own S-expression layer instead of using `kiutils`.

## Development

```bash
.venv/bin/pytest          # about 1270 tests, roughly 50 minutes (it runs KiCad for real)
.venv/bin/ruff check .
.venv/bin/mypy
```

Tests that need KiCad skip with an explicit reason when it is absent, so the suite
runs headless in CI. `AIPCB_FULL_CORPUS=1` runs the lossless round-trip test
against all ~16,000 KiCad files on the machine instead of a 400-file sample.

# 0003 — Generated schematics are netlist-first

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M2

## Context

`aipcb build` has to turn a flat netlist into a `.kicad_sch` that KiCad accepts,
ERC passes, and a human can open. The source format deliberately holds no
geometry, so the compiler decides where everything goes.

The brief allows naive placement — "correctness over beauty" — but says nothing
about how pins get connected, and that choice matters more than placement does.

## Options

1. **Route wires between pins.** Compute a path on the sheet from each pin to each
   other pin on its net.
2. **Net labels on pin stubs.** Give every pin a short stub ending in a label
   carrying its net name, and let KiCad join same-named labels.
3. **Buses and hierarchical sheets** mirroring the module structure.

## Decision

**Net labels on stubs** (option 2), with symbols on a grid grouped by module
instance.

## Rationale

Option 1 makes correctness depend on placement. Wires have to avoid symbols, avoid
each other, and meet pins exactly; every one of those is a chance to produce a
schematic that *looks* connected and is not, or that is connected differently from
what the source says. It also makes a small source change cascade into a completely
different sheet, which ruins the diffs the whole project is built around.

Option 2 decouples the two entirely. Connectivity comes from the label text, which
comes straight from the net name, so it is correct by construction no matter where
a symbol sits. Placement can then be as naive as the brief permits without putting
correctness at risk — and when placement improves later, connectivity cannot
regress. This is also how engineers draw dense schematics by hand, so the result
reads normally rather than looking machine-made.

Option 3 is the right answer eventually — hierarchical sheets would mirror the
module structure the source already has — but it is a large amount of work for no
correctness gain, and hierarchical labels have their own connectivity subtleties.
Deferred.

## Consequences and the details that mattered

* **Everything snaps to KiCad's 1.27 mm connection grid.** An off-grid pin is legal
  but unusable: a human cannot draw a wire that meets it. This was not theoretical
  — the first generated schematic put cell centres on a half-grid offset and KiCad
  reported 14 off-grid endpoints.
* **Derived symbols are flattened.** A `.kicad_sch` embeds copies of the symbols it
  uses, and KiCad will not chase an `extends` to a base symbol that is not there.
  `ATtiny85-20P extends ATtiny25V-10P`, so the copy is merged: the base's geometry
  and pins, the derived symbol's properties, and nested unit bodies renamed to keep
  KiCad's `<symbol>_<unit>_<style>` convention intact.
* **`PWR_FLAG` only on rails KiCad considers undriven.** A flag on a rail that
  already has a power-output pin is itself an ERC error, so the decision is made
  from the *symbols'* pin types rather than the part database's. The two can
  legitimately differ: our part definition describes an ISP header's VCC pin as
  sourcing power, while the stock `Conn_01x06` symbol calls every pin passive, and
  ERC only ever sees the symbol.
* **A `.kicad_pro` is emitted.** Without one KiCad ignores the project-local
  library tables, and every symbol is reported as coming from an unknown library.
  The project file is also where net classes have to live, so layer 2's
  `net_classes:` lands there as real KiCad design rules.
* **No date in the title block.** KiCad writes today's date; we do not, because a
  timestamp would make every rebuild a diff.

## Acceptance

For all three examples, against KiCad 9.0.8:

* `kicad-cli sch erc --severity-all` reports **0 violations**;
* the netlist `kicad-cli sch export netlist` extracts matches the source **net for
  net and pin for pin** — this is the test that actually proves correctness, since
  ERC would happily pass a schematic that connects the wrong things;
* `kicad-cli sch export pdf` renders, which exercises the embedded symbol graphics
  that ERC never looks at;
* two builds produce byte-identical files.

All four are enforced in `tests/test_schematic.py` and skip with a clear message
when KiCad is absent.

---

## Amendment (M14): placement is computed, and the sheet is a view

* **Status:** Accepted
* **Date:** 2026-08-22
* **Context:** milestone M14a–M14c

The decision above stands unchanged: connectivity is names, not wires. What M14
changes is everything *else* about the sheet, plus one thing this ADR never said out
loud.

### The bar moved, and why now

M2's bar was "correctness over beauty", and the grid placement it licensed was the
right trade at the time. It stopped being the right trade when `examples/pcie-sata`
needed a human review against a controller's reference design before fab. A netlist
with coordinates cannot be reviewed; it can only be re-read.

### What decides placement now

The source already carried everything needed and none of it reached the sheet:
`role`, `for:`, module structure, and the netlist graph itself. Placement is now
derived from them, in `aipcb.compile.sheet`:

1. Satellites (`role: decoupling` and its relatives) attach to the component their
   `for:` names, or -- for a pull-up with no `for:` -- to the component sharing the
   net it biases.
2. Module instances become visual clusters, drawn with a dashed frame and their
   instance name.
3. Blocks are ranked left to right by breadth-first distance from the design's
   *input* connector, over signal nets only. Power and ground are excluded from that
   graph deliberately: they touch everything, and a graph in which everything is
   adjacent has no layers.
4. Within a rank, the barycentre heuristic swept both ways.
5. Extents are measured from the symbols actually in use, including the room a power
   symbol or a net label needs.

The one non-obvious step is (3)'s seed. Seeding from *every* connector -- which is
what "connectors on the left" reads like at first -- puts a SATA port in the same
column as the PCIe edge connector it is downstream of, and the sheet then says the
opposite of what the board does. The seed is the connector that looks like an
input: an `edge_connector` if the source names one, otherwise whichever connector
carries the most rail pins.

### Power symbols replace labels on rails, where there is room

A rail or ground pin now gets a KiCad power symbol rather than a text label. A power
symbol names its net after its `Value`, so this carries exactly the connectivity the
label carried -- verified against KiCad's own netlister on all eleven examples, name
for name as well as pin for pin.

Two qualifications, both measured rather than assumed:

* **Not on a crowded side.** A ground symbol with its name beside it is wider than a
  2.54 mm pin pitch. Standing one on every pin of a six-way header puts three net
  names on top of three others, which is worse than the labels it replaced. A side
  whose pins are closer together than a power symbol is wide keeps its labels.
* **A net named like a stock symbol gets that symbol.** `GND` gets `power:GND`,
  `+3V3` gets `power:+3V3`. `P3V3` has no stock symbol and gets the generic rail
  shape with its own name on it, which is what a person does by hand.

### Stacked pins are drawn once

KiCad's own libraries stack a part's repeated power pins on one coordinate --
`Connector:Bus_PCI_Express_x1` draws its nine grounds as a single pin. Emitting a
stub and a label per pin produced nine identical labels in one place, which accounted
for all 21 of the overlaps still left on the pcie-sata sheet. One point carrying one
net is one connection, and is drawn once; every pin at that coordinate is joined by the one
wire, which is precisely why the library draws them that way.

### Manual edits to a sheet: policy (b), stated

**The finding.** M6's preserve mechanism does not cover `.kicad_sch` at all, and
never did. `build_design` wrote the schematic with an unconditional
`_write_if_changed` -- no read of the existing file, no merge, no warning. Manual
edits to a generated schematic were silently overwritten on every rebuild. That is
the "silent third thing" M14c forbids.

**The decision.** Manual schematic layout is **not supported**, and a rebuild that
is about to discard one says so.

Extending M6's UUID-and-fingerprint machinery to the sheet was considered and
rejected, on two grounds. First, everything on the sheet is generated -- unlike a
board, there is nothing on it that is not ours, so there is no class of element for
which "preserve the human's version" is even meaningful. Second, and decisively:
after M14a the sheet's layout is a *computed global property* of the flow. Pinning a
few symbols where a human left them while the rest re-flow around them produces a
drawing that is neither the human's nor the generator's. On a board, position is
engineering -- impedance, mechanics, thermals -- and preserving it is obviously
right. On a sheet, position is presentation.

So: **the YAML is the reviewable source; the sheet is a view of it.**

**The mechanism.** Every generated sheet carries a hash of its own contents in
title-block comment 9 (`aipcb-sheet:<digest>`). On rebuild, a sheet whose stamp no
longer matches its contents has been edited, and `schematic-edits-discarded` is
reported before it is regenerated. A sheet with *no* stamp answers "unknown" rather
than "edited" -- it was written by something else, and those are different sentences
deserving different words. Tested in `tests/test_manual_routing.py`.

### Rendered review artefacts

`aipcb build --render` plots every sheet to `review/<name>.pdf` and `.svg` via
`kicad-cli sch export`, and writes `review/readability.json`: overlapping drawn
items, wire crossings, wires through symbols, total wire length, and the mean and
maximum distance from each decoupling capacitor to the nearest pin of the IC it is
declared `for:`. Those are the numbers M14 was argued from, and they are measured
off the `.kicad_sch` rather than taken from the planner, so the same measurement runs
against a sheet built by any version of this tool.

### What did not change

* Connectivity is still names on stubs, and the netlist KiCad extracts from all
  eleven examples is identical to the pre-M14 one -- net for net, name for name and
  pin for pin.
* ERC is still 0 violations on all eleven.
* Output is still byte-identical between two builds of an unchanged design.
* Hierarchical sheets are still deferred. No bundled example comes near a component
  count that would justify splitting one, so multi-sheet output would have shipped
  untested against any real design; the threshold and the split are on the roadmap
  with that reason attached.

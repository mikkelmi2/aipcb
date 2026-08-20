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

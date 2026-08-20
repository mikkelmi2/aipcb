# 0004 — Board generation: copy footprints, cluster by intent

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M3

## Context

M3 emits a `.kicad_pcb` with footprints placed, an outline, a stackup, and net
classes mapped to KiCad's. Two questions had to be answered: how footprint
definitions get into the board, and how anything decides where a part goes.

## Decision 1 — footprints are copied, not modelled

A footprint's definition is read from KiCad's library and copied verbatim into the
board, then adapted: renamed to its full library id, given a position, a UUID and a
`path` back to its schematic symbol, and its pads attached to nets.

The alternative — modelling footprints as typed objects and re-emitting them — is
the mistake ADR 0001 already rejected for files. A footprint carries pads, silkscreen,
courtyards, fabrication layers, 3D model references and manufacturer metadata, and
anything the model does not know about is lost. Copying keeps all of it, including
constructs added by future KiCad versions.

Two details cost a debugging cycle each, and both produce a board that KiCad
refuses to load with no indication of what is wrong:

* **The library id must remain the footprint node's first token.** Inserting the
  `layer`/`uuid`/`at` header at the front of the node pushed the name after them.
* **Only drawable items may carry a UUID.** Stamping one onto `layer`, `descr`,
  `tags` or `attr` is fatal. UUIDs belong on `fp_*` items, pads, zones and groups.

## Decision 2 — placement clusters by intent

The source contains no coordinates, but it does contain relationships: `for:`
references, `group` constraints, `max_distance` constraints. Placement turns those
into geometry:

1. a union-find gathers components joined by any of those relationships into
   clusters (`keep_apart` is deliberately excluded — it is a reason to separate);
2. each cluster is packed into a compact grid of its own;
3. clusters are shelf-packed into the usable board area, tallest first.

This is not an optimiser, and the brief does not ask for one. What matters is that
it is deterministic and that it *uses the intent the format exists to capture* —
a decoupling capacitor ends up beside its chip because the source said `for: U1`,
not because a heuristic guessed. The tests assert exactly that.

Part sizes come from the footprint's **courtyard** layer where it has one, since a
courtyard is precisely the manufacturer's statement of the space a part needs. Pad
extents are the fallback, plus a margin, because they underestimate a part whose
body overhangs its pads.

## Decision 3 — global labels, so net names survive

Schematics originally used local labels. KiCad names their nets after the sheet
they sit on, so a label `VBUS` on the root sheet becomes the net `/VBUS`. The board
then has to use the prefixed name or `--schematic-parity` reports a `net_conflict`
on every pad — 31 of them on the USB example.

Switching to global labels removes the prefix. The board's net names are then
exactly the names written in the source, which matters well beyond parity: it is
what lets M4 map a DRC violation on a net straight back to the line that declares
it, with no name rewriting in between. A flattened netlist has no sheets to scope
anything to, so nothing is lost.

## Decision 4 — unconnected pads get KiCad's synthetic nets

KiCad does not leave an unconnected pad netless; it invents a net per pin, named
`unconnected-(U1-XTAL1{slash}PB3-Pad2)`. Leaving those pads bare makes the board
disagree with the schematic and parity reports each one. We reproduce the
convention, including the `/` → `{slash}` escaping.

## Not done: back-side placement

`side: back` is parsed and validated, but a footprint placed on the back must have
every `F.*`/`B.*` layer pair swapped and its local geometry mirrored. That is
deferred rather than approximated: a subtly wrong mirror produces a board that
looks plausible and is unbuildable. The build reports a warning naming the
component and saying it has been placed on the front instead.

## Acceptance

For all three examples, against KiCad 9.0.8:

* the board loads, and `kicad-cli pcb drc --severity-all` reports **0 violations**;
* `--schematic-parity` reports **0 issues**;
* the only unconnected items are ratlines, which is the expected state before
  routing;
* `kicad-cli pcb export pdf` renders;
* two builds produce byte-identical files.

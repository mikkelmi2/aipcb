# 0005 — Incremental builds: the source owns what it declares

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M6

## Context

`aipcb` generates boards, but people finish them. Someone opens the `.kicad_pcb`,
nudges a connector to line up with an enclosure, routes a differential pair by
hand, pours a ground zone. A rebuild that discarded all of that would make the
toolchain unusable for anything past the first afternoon — and would make the whole
"KiCad as the backend, source as the truth" premise a lie, because in practice
nobody could afford to re-run the compiler.

The brief states the rule: preserve manual work for elements whose source is
unchanged, and let source changes win for the elements they own.

## Decision

**The source owns what it declares; everything else belongs to the person who drew
it.** Applying that needs two questions answered per element.

### Is this the same element? — by UUID

Ours are hashes of source paths, so a footprint's identity survives rebuilds,
edits elsewhere in the file, and any reordering. A UUID we do *not* recognise is by
definition somebody else's work, and is preserved untouched. This is the third
distinct job the deterministic-UUID scheme does, after diff stability (M2) and
violation mapping (M4), and the third reason ADR 0001 could not accept a library
that rewrites them.

### Has the source changed its mind? — by fingerprint

Every generated footprint carries `aipcb.fingerprint`: a hash of the source facts
that determine it — its part, its footprint, its pin-to-net connections, its side,
and any placement constraint or rule that names it. On rebuild, if that hash still
matches, the source has nothing new to say and the human's position stands. If it
differs, the source has changed and the freshly computed placement wins.

The fingerprint deliberately covers *less* than the whole component. A changed
`reason:` or a corrected typo says nothing about placement, and shoving a
hand-placed part back onto the grid because someone improved a comment would make
the feature worse than useless. There is a test for exactly that.

## What is preserved

| Thing | Rule |
|---|---|
| Footprint position, rotation, side | Kept when the fingerprint matches |
| Dragged reference/value text | Kept with the footprint |
| Tracks, arcs, vias, zones, groups, dimensions, added graphics | Always kept — we never generate them |
| Board outline | Kept **only if the source declares no `layout.outline`** |
| Footprint of a deleted component | Removed |
| Copper on a net the design no longer has | Removed, with a warning |

Two of those deserve their reasoning stated.

**The outline.** Drawing the board edge in KiCad is a normal workflow — that is
where the mechanical constraints are visible. A design that says nothing about its
outline has not claimed ownership of it, so whatever is there stays. A design that
declares one owns it.

**Orphaned copper.** A track belonging to a net that no longer exists is not
harmless history; it is a short waiting to happen, and keeping it would mean the
board no longer matches the schematic. It is dropped, and the drop is reported as a
warning rather than done quietly.

## Failure modes

An existing board that cannot be parsed is **not** silently overwritten. It might
hold hours of somebody's routing, so the build reports what went wrong and says the
file will be replaced, leaving the decision with the person who can still move it
aside. `aipcb build --fresh` regenerates from source and discards manual work, for
when that is what is actually wanted.

## Export

`aipcb export` completes the path the project set out to build: source in one end,
a package a board house can quote from out the other, with no step in between that
required opening a GUI. It produces Gerbers for every copper and technical layer,
Excellon drill files with a drill map, a `.gbrjob` job file, a grouped BOM, and a
placement file — all via `kicad-cli`, so the bytes are exactly what KiCad itself
would plot.

The BOM comes from the schematic rather than the board, because that is where the
fields a BOM needs live; it carries the descriptions written in the component
database, so the part list a fab receives says what each part actually is.

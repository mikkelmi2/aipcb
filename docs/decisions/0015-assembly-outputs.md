# 0015 — What an assembler is sent, and the one part of it this project will not guess

* **Status:** Accepted
* **Date:** 2026-08-24
* **Context:** milestone [M21](../milestones/m21-assembly-outputs.md). Extends the
  fabrication export in [`export.py`](../../src/aipcb/compile/export.py) and the
  part schema in [`parts.py`](../../src/aipcb/model/parts.py). The delivery report
  is [`m21.md`](../reports/m21.md).

## 1. The gap analysis, which is where this milestone started

M21's brief carried a correction: `aipcb export` already advertises "a BOM and a
placement file", so the milestone was likely hardening rather than building. The
audit says: **the files exist, and neither is an assembly file.**

| | what shipped before M21 | what an assembler needs |
|---|---|---|
| BOM | `kicad-cli sch export bom`, columns `Reference,Value,Footprint,QUANTITY,Description` | fab-specific column names; a manufacturer part number; the fab's own part id |
| BOM grouping | `--group-by Value,Footprint`, designators compressed to ranges (`J2-J5`) | one line per purchasable thing; designators listed, not ranged (§2.3) |
| placement | `kicad-cli pcb export pos`, columns `Ref,Val,Package,PosX,PosY,Rot,Side` | fab-specific column names; `Top`/`Bottom`; rotation in the fab's range |
| what is in it | every footprint KiCad does not exclude | only what a machine places, and PCBWay only what it places *by reflow* |
| procurement | `Supplier.manufacturer`/`.mpn` existed in the model and **reached nothing** — only `datasheet` was emitted | the whole point of a BOM |
| DNP | a `dnp` flag reached the schematic and the board | must reach both assembly files |

So the two existing files are correct KiCad output and neither is orderable. They
are kept, unchanged and byte-identical, because they are what the *fabricator*
package has always contained and nothing in this ADR is a reason to move them. The
assembly files are new, additive, and off unless asked for.

## 2. What each assembler actually asks for

Measured from each fab's own published requirements on 2026-08-24, not inferred
from somebody's exporter.

### 2.1 JLCPCB

* **BOM** — `.csv`, `.xls`, `.xlsx`. Required: `Comment`, `Designator`, `Footprint`.
  Their KiCad guide adds `JLCPCB Part #`, which is the LCSC id. Maximum 200
  designators per line; no duplicate designators anywhere in the file.
* **CPL** — `.csv`, `.xls`, `.xlsx`. Columns `Designator`, `Mid X`, `Mid Y`,
  `Rotation`, `Layer`. **Units: millimetres.** `Layer` is `Top`/`Bottom`. Both
  sides in one file.
* **Rotation** — *"The rotation of the component given in degrees. Positive values
  are counter clockwise."*
* **Cross-file rule** — designators must match between the two files exactly, and
  case-sensitively: *"the letter case of reference designators must be consistent
  between the BOM and CPL files (e.g. `R1` ≠ `r1`)"*. Only components appearing in
  both are recognised.
* **DNP** — not documented. Their pages say nothing about how to mark a part that
  must not be fitted.

### 2.2 PCBWay

* **BOM** — `.xls`, `.xlsx`, `.csv`. For a turn-key order: `Line#`,
  `Quantity Per Part Number`, `Reference Designator`, `Part Number`,
  `Part Description`, `Package`, `Type` (*"Surface mount, Thru-hole or Hybrid"*),
  `Manufacturers Name`, `Manufacturers Part Number`, `Distributors Part Number`.
* **Centroid** — reference designator, X, Y, rotation, side (`Top`/`Bottom`), and
  one rule JLCPCB does not state: **"Only surface mounting parts are listed in the
  Centroid."**
* **DNP** — not documented.

### 2.3 What that means for the design

Three findings shaped the implementation:

1. **The two fabs disagree about the file's contents, not just its column names.**
   PCBWay's centroid excludes through-hole parts; JLCPCB's documentation does not.
   So a format is not a rename — it carries rules — which is why
   [`FORMATS`](../../src/aipcb/compile/assembly.py) holds `centroid_is_smt_only`
   beside the column tuples.
2. **Neither fab documents a DNP convention.** A part that must not be fitted is
   therefore said in the column a human reads: the BOM `Comment` gains
   `(DNP - DO NOT POPULATE)` and the part never reaches the centroid. Marking it
   only in a machine column nobody has specified would be worse than saying it in
   words, because a silent DNP is a fitted part.
3. **JLCPCB's documented limits are checked, not just its columns.** A
   bill-of-materials line carrying more than 200 designators is a rejected upload,
   so it is a warning that names the line rather than a file the fab bounces.
4. **Designators are listed, never ranged.** KiCad's own BOM compresses `J2,J3,J4,J5`
   to `J2-J5`. Neither fab documents range notation, JLCPCB matches designators
   between the files literally, and a range that is not expanded is four parts that
   do not match. Listing them costs nothing and cannot be misread.

## 3. Coordinates: the origin, and why they are not computed twice

The centroid is derived from the placement CSV `kicad-cli pcb export pos` already
writes, and not from a second reading of the board.

That file is exported with `--use-drill-file-origin`, as the Gerbers and the drill
file are. Measured on `examples/pcie-sata`: the board's `aux_axis_origin` is
`(100, 145)` in KiCad's own frame, `U1` sits at `(140, 119)`, and the placement file
records `(40, 26)`. So the transform is

> `x_file = x_kicad − origin_x`,  `y_file = origin_y − y_kicad`

— the origin subtracted, and **Y inverted**, because KiCad measures down from the
sheet corner and the placement file measures up from the drill origin. Nothing in
this project reimplements that. The decision is that the assembler's file and the
fabricator's files are two views of one measurement, and the cheapest way to
guarantee that is to make one of them a transcription of the other.

The overlay in `overlay_svg` is the only place the transform is written down, and it
is written down there because the overlay draws the *board outline* — which comes
from the board — into the *placement file's* frame.

## 4. Rotation: the convention is implemented, the correction is not, and that is the decision

**The convention needs no transform.** Both fabs document degrees, counter-clockwise
positive. KiCad writes degrees, counter-clockwise positive. The only difference is
range — KiCad signs them, the fabs' samples run 0–360 — so the value is folded into
`[0, 360)` and **no part turns**.

**The correction is a different thing, and this project does not ship one.** What
breaks real boards is not the convention but the *zero reference*: a fab whose parts
library holds a package at a different orientation from the KiCad footprint needs a
per-package offset, and the failure is silent — a QFN or SOT-23 soldered 90° out,
pin 1 in the wrong corner.

Three things were established about that correction, and together they are the
reason it is not implemented here:

1. **Neither fab publishes it.** JLCPCB documents the convention and not the
   offsets. There is no authoritative table to verify an implementation against,
   and M21's guardrail is explicit that the mapping must be verified against the
   fab's documented convention rather than inferred.
2. **It is not algorithmic.** The community tooling that solves this — KiBot's
   `rot_footprint` filter, `kicad-jlcpcb-tools`' rotation database — is a lookup of
   regular expressions over footprint names, forty-odd entries long, maintained by
   hand.
3. **It is not even a function of the footprint.** KiBot's own documentation says
   so plainly: *"you can have two components with the same footprint and different
   rotations in the same project."* A table keyed by footprint is therefore a
   heuristic by construction, and a heuristic that is wrong is worse than no table,
   because a wrong offset looks exactly like a right one until the board comes back.

There is a fourth reason and it is this project's standing one: those tables are
other projects' work under their own licences, and copying data out of them is the
same act as copying code out of them.

**So: the convention is implemented exactly, no correction table ships, and the
verification is a picture.** `aipcb export --assembly` renders a placement overlay
per side, drawn *from the centroid file's own rows* — every part where the file puts
it, turned the way the file turns it, with a dot marking where pin one lands after
that rotation. A file can say `180` and look perfectly ordinary; the cheap way to
notice a backwards diode is to see its cathode on the wrong end. When a part does
need an offset, it is declared on the part, and the overlay is how a person
convinces themselves it worked before spending money.

## 5. `assembly:` is measured, not defaulted

M21's brief proposed `assembly: smt | tht | dnp | none` defaulting to `smt`. The
enum is implemented as specified; **the default is not**, and the reason is a
measurement on this repository's own corpus.

Four bundled examples are built around through-hole parts — the breakout headers in
`congestion`, the DIP-8 in `mcu-4layer` whose two rows of pins are the obstacle that
board exists to route around, the pin headers in `backplane`, the power header in
`ldo-supply`. A default of `smt` would have told PCBWay to reflow all of them, and
the error would have been silent in exactly the file whose job is to catch it.

So an undeclared `assembly:` is read off the footprint KiCad actually placed: its
`(attr ...)` flags, and where those say nothing, the pad types. A declaration always
wins. Verified on `mcu-4layer`, where `J2` (a 1×06 pin header) and `U1` (a DIP-8)
are detected as through-hole, appear on the bill, and are correctly absent from
PCBWay's centroid.

`none` is a fourth state the brief did not have and the corpus needed: a card-edge
finger field and a mounting hole are on the board, are on a net, and are neither
bought nor placed. Without it, `examples/pcie-sata`'s gold fingers appeared on the
bill of materials as a line an assembler cannot source.

## 6. The schema has two spellings for one field, deliberately

`Supplier.manufacturer` and `Supplier.mpn` predate this milestone and two bundled
libraries already use them. M21a's brief puts `mpn:` and `manufacturer:` at the top
level of a part. Both now validate, `Part.procurement()` folds them, and declaring
the same field twice with *different* values is an error rather than a silent
winner.

That is a wart and it is the cheaper of the two available warts: the alternative was
to break every part file that already carries `supplier:`. The top-level spelling is
the current one and is what documentation shows.

## Decision

1. **Keep the existing Gerber, drill, BOM and placement export unchanged and
   byte-identical.** The assembly files are additive and produced only on request.
2. **Derive the centroid from KiCad's placement file**, never from a second reading
   of the board, so the assembler's coordinates and the fabricator's are one
   measurement.
3. **Drive the bill of materials from the netlist**, not from the placement file, so
   that a part KiCad excludes from position files is still something you can buy.
4. **Implement each fab's format as columns plus rules**, from its own published
   requirements, with the sources recorded in §2.
5. **Implement the rotation convention exactly; ship no correction table.** Provide
   the overlay as the verification, and say in the documentation that a package
   needing an offset must declare it.
6. **Measure `assembly:` off the footprint when it is not declared**, and add
   `none` for footprints that are not components.
7. **Refuse to write an assembly package for a board with back-side parts.**
   `side: back` validates, warns, and places on the front (M9), so a back-side
   centroid would not describe the board that was built.

## Consequences

**What this buys.** A design whose parts carry procurement data produces an order
package in one command, and one whose parts do not is *told which parts*, by
designator, rather than being left to find out at the assembler.

**What it does not buy, and this is the honest part.** A clean export is not a
guarantee that the order is correct. Three gaps survive this ADR and are named so
that nobody reads a green run as more than it is:

* **Rotation offsets are unverified until a board comes back.** The overlay reduces
  the risk to what a human can see; it does not eliminate it. The first assembled
  order of any design is the test.
* **Nothing checks that an MPN is *the right part*.** The tool checks that a string
  is present. A wrong part number exports as cleanly as a right one, which is why
  §4's rule against guessing applies to part numbers too and why
  `examples/pcie-sata`'s controller has none.
* **Two-sided assembly is unimplemented, not merely untested.** The guard in
  decision 7 is a refusal, and it stays until `side: back` is implemented.

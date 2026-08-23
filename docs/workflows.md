# Working in KiCad alongside aipcb

Everything in this document already worked before M14. What did not exist was this
page. Preserve landed in M6, `sync-placement` in M9d, hand-over in M9f — three
halves of a human-in-the-loop workflow that was never written down as one, so the
only way to discover it was to read three delivery reports.

The rule underneath all of it is one sentence, and it is worth having in mind before
any of the recipes below:

> **The source owns what it declares; everything else belongs to the person who
> drew it.**

That is [ADR 0005](decisions/0005-incremental-builds.md). What follows is what it
means at a keyboard.

---

## What is preserved, and what is not

| Thing | On `aipcb build` |
|---|---|
| Footprint position, rotation, side | **kept**, when the source has not changed its mind about that part |
| Tracks, arcs, vias | **always kept** — aipcb's router adds copper, it never owns yours |
| Zones you drew, groups, dimensions, added graphics | **always kept** |
| Board outline | **kept**, unless the source declares a `board.outline` |
| Copper on a net the design no longer has | **removed**, with a warning |
| Footprint of a deleted component | **removed** |
| Anything in the `.kicad_sch` | **regenerated**, with a warning if you edited it |

The last row is the one that differs from the others, and it is deliberate. See
[The schematic is a view](#the-schematic-is-a-view) below.

"Has the source changed its mind?" is answered by a fingerprint stored on each
generated footprint: a hash of the part, the footprint, the pin-to-net connections,
the side, and any placement rule naming it. Fixing a typo in a `reason:` does not
move a part you positioned by hand. Changing its footprint does.

---

## Moving a part in KiCad

Drag it, save, rebuild. It stays where you put it.

```bash
$EDITOR board.kicad_pcb     # or KiCad, obviously
aipcb build design.yaml     # your position survives
```

Unless the source *declares* where that part goes. A `placement:` entry is
mechanical law — a connector has to line up with the hole in the enclosure whatever
looked neater on screen — so a rebuild puts it back and says so:

```
warning[fixed-placement-drift]: moved back to where the source fixes it: J1 by 2.30 mm
  hint: a `fixed:` placement is mechanical law, so the source wins. If the board is
  right and the source is stale, run `aipcb sync-placement` to write the new
  position into the YAML
```

When the board is right and the YAML is stale, the drift goes the other way:

```bash
aipcb sync-placement design.yaml            # list what moved
aipcb sync-placement design.yaml --apply    # write the new positions into the YAML
```

`--apply` edits the YAML in place, keeping every comment and every `reason:`. It
asks per part unless you pass `--yes`.

---

## Routing by hand

Three modes, and the source can say which one it is in. This is the M14d feature:
before it, "manual" was something that *happened*; now it is something you
*declare*.

### Full auto

```bash
aipcb route all design.yaml
aipcb check design.yaml
```

### Hybrid — you route the critical nets, aipcb routes the rest

Declare the nets that are yours, on a class or one at a time:

```yaml
net_classes:
  rf:
    trace_width_mm: 0.4
    routing: manual        # every net in this class is yours
    description: Drawn by hand against the antenna's reference layout.

nets:
  ANT_FEED:
    class: rf
  SENSE:
    class: analog
    routing: manual        # just this one
  CLK_OUT:
    class: rf
    routing: auto          # ...and this one opts back out of its class
```

Then:

```bash
aipcb route all design.yaml   # your nets are left alone, and listed as pending
# ...route them in KiCad, or send them to an external router...
aipcb check design.yaml       # checks all the copper identically, whoever drew it
```

`aipcb check --json` and `aipcb route all --json` put every net in one of four
states under `summary.nets.counts`:

| State | Meaning |
|---|---|
| `manual-routed` | declared manual, and copper for it is on the board |
| `manual-pending` | declared manual, and **there is no copper yet** |
| `auto-routed` | aipcb's router laid it |
| `handed-over` | aipcb's router tried and refused; the reason and the blocking corridor are given |

**One thing `routing: manual` does not turn off.** A *pattern generator* is not the
router. If a net is declared manual and also lands on a package with a `fanout:`
block, or is half of a declared pair `transitions:` entry, that pattern is still
generated — the source asked for that shape by name, and honouring `routing: manual`
there would silently disable the other declaration. You get the escape stub and
nothing else; the rest of the net is yours. It is reported, so it is never a
surprise:

```
info[manual-net-has-generated-pattern]: IO_PD0 is declared `routing: manual` and
also carries copper from a declared pattern (a fanout escape or a pair via transition)
```

`manual-pending` is the state worth watching. It is where a board sits between "these
pairs are mine" and "I have drawn them", and to anything that only counts unrouted
connections it looks exactly like a finished board. `aipcb check` warns about it by
name and `summary.nets.manual_pending` lists them.

### Fully manual

Declare `routing: manual` on every class, or simply never run `aipcb route all`.
`aipcb build` generates the board with footprints placed and no copper; you draw all
of it; `aipcb check` verifies it.

```bash
aipcb build design.yaml
$EDITOR board.kicad_pcb
aipcb check design.yaml --no-route
```

`--no-route` checks the board as it stands rather than routing it first.

---

## When the router hands something over

The router refuses to deliver marginal geometry. When it cannot make a connection
legally it says which one, where the board ran out of room, and which nets own the
capacity that was contested:

```
  unrouted (over_complexity): PRSNT J1.A1 -> J1.B17
```

That is a hand-over, not a silent gap. Finish it in KiCad, and the next build
preserves what you drew — the copper is yours now, and the router routes around it.

---

## The schematic is a view

Board and schematic are treated differently on purpose.

A board carries things aipcb never generates — your tracks, your zones, your
dimensions — so preserving them per element is both possible and obviously right. A
schematic does not. Every symbol, wire, label and power symbol on the sheet is
generated from the source, so there is nothing on it that a rebuild could sensibly
hand back.

There is a second reason, and it is the stronger one. Since M14 the sheet's layout
is *computed* — from the signal flow, the module structure, the `for:` references
and the roles. Pinning a few symbols where a human left them while the rest re-flow
around them produces a drawing that is neither the human's nor the generator's. On a
board, position is engineering: impedance, mechanics, thermals. On a sheet, position
is presentation.

So: **the YAML is the reviewable source; the sheet is a view of it.** Move a symbol
in KiCad and the next `aipcb build` regenerates it — but never silently:

```
warning[schematic-edits-discarded]: led-blinker.kicad_sch has been edited since aipcb
generated it, and those edits are about to be regenerated away
  hint: the schematic is a view of the design, not a second copy of it: move the
  change into the YAML, where it is reviewable and survives. Copy the sheet aside
  first if you want to keep it
```

The detection is a hash of the sheet's own contents, stored in the sheet (title-block
comment 9). A file with no stamp is not reported as edited — it was written by
something else, and "somebody edited this" and "I do not know who wrote this" are
different sentences.

If you want a snapshot of a sheet, render one:

```bash
aipcb build design.yaml --render     # review/<name>.pdf, .svg and readability.json
```

---

## Reviewing a schematic

```bash
aipcb build design.yaml --render
```

writes into `review/`:

* `<name>.pdf` and `<name>.svg` — what a reviewer actually reads, plotted by KiCad
  itself;
* `readability.json` — the numbers behind the drawing: overlapping items, wire
  crossings, wire length, and how far each decoupling capacitor sits from the pin it
  is declared `for:`.

The sheet reads left to right: connectors and inputs on the left, controllers in the
middle, outputs on the right. Module instances are drawn as named, dashed clusters.
Rails point up, grounds point down. A decoupling capacitor stands at the IC it
decouples, grouped with the others on its rail.

---

## See also

* [`docs/external-routers.md`](external-routers.md) — routing declared-manual nets
  with Freerouting or another external router, headlessly, from an agent.
* [ADR 0005](decisions/0005-incremental-builds.md) — what preserve does and why.
* [ADR 0003](decisions/0003-schematic-generation.md) — why the schematic is
  generated the way it is, including the M14 amendment on manual edits.

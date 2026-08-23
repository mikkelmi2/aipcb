# aipcb

**A circuit board described as intent, compiled to KiCad.**

You write what the circuit *is* — in YAML. `aipcb` compiles it into real KiCad
schematics and boards, places, routes, pours, runs KiCad's own ERC and DRC on the
result, and can hand each pair to a field solver. Every problem points back at the
source line that caused it, and your KiCad edits survive the next build.

![examples/pcie-sata, rendered by KiCad from aipcb's own fabrication output](docs/images/06-3d.png)

That is `examples/pcie-sata`, bundled here — a PCIe x1 card, four-port SATA, eleven
controlled-impedance pairs, four layers. Every image below is generated from it by
[`tools/make_images.py`](tools/make_images.py).

## What fifteen milestones hardened

**Deterministic compilation.** The same YAML gives byte-identical KiCad files —
rebuild unchanged and the file is not even rewritten. Every UUID is derived from
the source, which is what maps each element back to the line that asked for it.

**Verification you can trust.** ERC and DRC are run by KiCad itself, never
reimplemented, and come back as source-referenced diagnostics. Three invariants
guard the tool that produces them: `check` is a function of its source, no two
nets' copper may overlap, no simulation slice may be built with unbonded copper or
an unreachable reference plane. Each was learnt from a real defect, and
[`docs/reports/`](docs/reports/) tells the stories.

**Schematic generation.** Sheets drawn by convention from what the source already
knows — what serves what, which rails go up — proven identical to the source
netlist pin for pin, and ERC-clean.

**Manual layout is first-class.** `preserve` carries your KiCad edits across
rebuilds, `sync-placement` writes parts you moved back into the source,
`routing: manual` marks a net as yours, and the Freerouting bridge checks what
another router sends back.

## Maturity at a glance

| | | |
|---|---|---|
| Source format, compilation, `check`, schematics | **stable** | [`format.md`](docs/format.md) |
| Manual layout — preserve, `sync-placement`, `routing: manual` | **stable** | [`workflows.md`](docs/workflows.md) |
| Part placement | **basic** — shelf-packed clusters, unoptimised on purpose | [`roadmap.md`](docs/roadmap.md#placement) |
| Autorouting | **beta** — finishes this corpus, has never met a board it did not design | [`topology.md`](docs/topology.md) |
| SI simulation | **beta** — physically valid on ten of eleven links | [`m13.md`](docs/reports/m13.md) |
| Freerouting bridge | **new** — landed in M14e | [`external-routers.md`](docs/external-routers.md) |

## What beta means here

**The autorouter** finishes the corpus: `examples/pcie-sata` comes back **90 of 90
connections routed, 12 of 12 pairs coupled, 0 errors from KiCad's own DRC**, on
four layers. What it has not done is meet a board somebody else designed. The label
costs little: it **fails loudly**, naming each connection it could not finish
rather than leaving the board quietly short, and it is **optional** —
`routing: manual` and the Freerouting bridge take it out of the loop.

**SI simulation** extracts ten of the eleven flagship links physically; the
eleventh is reported unusable rather than plotted. It validates the layout you
declared, not the board a fabricator presses: it finds a pair routed too narrow, it
does not replace a test coupon, and it certifies compliance with nothing.

*Beta is a measurement, not a mood:* routing graduates once it has routed five
externally-contributed boards, passed a harder congestion stress example, and been
benchmarked against board size. The third of those is **done** — `aipcb bench`
records the whole corpus and CI diffs a subset on every change; routing cost grows
close to linearly in connections × triangulation size, and the numbers are in
[the M16 report](docs/reports/m16.md). The other two need a board this project did
not design. [The conditions in full](docs/roadmap.md#maturity-and-graduation).

## Source to fabrication, in six steps

### 1. Declare intent, not geometry

The width is *derived* from the impedance and the stackup — you never compute it:

```yaml
net_classes:
  pcie_tx:
    impedance_diff_ohm: 85    # the requirement; the trace width follows
    diff_pair_gap_mm: 0.15    # a manufacturing choice, so an input
    max_skew_mm: 0.127        # PCIe CEM r3.0 §4.7.7, for an add-in card
    reference: In1.Cu         # the plane the return current uses
```

### 2. It compiles to a schematic

![The pcie-sata schematic, rendered to A3](docs/images/02-schematic.png)

### 3. Parts are placed

Mechanically-fixed parts anchor the board, everything else is placed relative to
what it serves, and the outline is checked against the card-edge footprint's own
`Edge.Cuts` rather than drawn twice.

![The placed board, before any copper](docs/images/03-placed.png)

### 4. And routed

A topological router negotiating congestion across four layers. Coupled pairs route
as one object — through the AC-coupling capacitors, across layer changes, at the
derived width.

![The routed board, all eleven pairs coupled](docs/images/04-routed.png)

### 5. The pairs are simulated

`aipcb simulate` slices each pair out of the routed board with its reference plane,
meshes it, runs openEMS.

![Simulated differential impedance and worst return loss, for each of the eleven pairs](docs/images/05-simulation.png)

The honest version of that picture: **six of the eleven pairs come back more than
10% from the impedance they declared**. That is the step doing its job: the
closed-form width says one thing, the field solver says how wrong it was.

### 6. And exported for fabrication

`aipcb export` writes Gerbers, drill files, a BOM and a placement file — the render
at the top of this page is KiCad's, from that output.

## You do the layout, if you want

| Mode | You do | aipcb does |
|---|---|---|
| **Full auto** | nothing | places, routes, pours, checks |
| **Hybrid** | draw the nets you care about, in KiCad | routes the rest *around* your copper, never through it, and checks all of it identically |
| **Fully manual** | all the copper | schematic, placement, pours, ERC/DRC, high-speed checks, export |

Hybrid is the one most people want. Mark a net or a whole class as yours:

```yaml
nets:
  PCIE_TXP: { class: pcie_tx, routing: manual }   # this one is mine
```

`aipcb check --json` then reports every net as `manual-routed`, `manual-pending`,
`auto-routed` or `handed-over`. `manual-pending` is the one that matters: a board
whose critical pairs are declared-and-not-yet-drawn looks finished to anything
counting unrouted connections, and this names it. Or hand the board to another
router — [Freerouting, headlessly](docs/external-routers.md).

## Quickstart

Python 3.12+ and **KiCad 9**, the backend rather than an optional extra: compiling
reads its stock libraries, and check, render and export shell out to `kicad-cli`.

```bash
git clone https://github.com/mikkelmi2/aipcb && cd aipcb
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/aipcb validate examples/usb-port/design.yaml
```

About 7 seconds on a warm pip cache, and out comes `ok: no problems found` with a
one-line summary. Now build it and open the result in KiCad:

```bash
.venv/bin/aipcb build examples/usb-port/design.yaml
kicad examples/usb-port/usb-port.kicad_pro
```

Then break it on purpose — change `diff_pair: USB_DP` to `USB_DPP` under
`nets: USB_DM:` — and validate again. Located, coded diagnostics rather than a
stack trace is what makes the loop work:

```
examples/usb-port/design.yaml:53:3: error[asymmetric-diff-pair]: net 'USB_DP'
names 'USB_DM' as its differential partner, but 'USB_DM' names USB_DPP
  at: nets.USB_DM.diff_pair
  hint: set `diff_pair: USB_DP` on 'USB_DM'
[... and error[unknown-diff-pair] for the net 'USB_DPP' that does not exist]

2 errors
```

`--json` gives the same thing machine-readably. [`docs/format.md`](docs/format.md)
is the reference for every key; [`docs/workflows.md`](docs/workflows.md) is how the
loop is used.

## What it does not do

**Not built.** Dense BGA escape, DDR fly-by topologies, HDI (blind and buried
vias), six or more layers, multi-sheet schematics, buses. Not "coming soon" —
absent, and a board needing them hands over rather than guesses. The class shown to
finish is the one above: small, high-speed-critical, two or four layers, and
[`docs/roadmap.md`](docs/roadmap.md) says which of the rest are wanted.

**No board from this repository has been fabricated yet.** When one has, this
paragraph will say so with pictures.

## Documentation

[`docs/README.md`](docs/README.md) is the map, and repeats the tiers above.

**This project was built milestone by milestone by AI agents, and the whole
engineering record is in the repository** — every specification, thirteen decision
records, and a delivery report per milestone measuring what actually landed. Start
at [`docs/reports/`](docs/reports/): written for someone who was not there, and
candid about what did not work.

Contributions are welcome — [`CONTRIBUTING.md`](CONTRIBUTING.md) says how. A board
this did not design is the most useful thing you can bring.

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

# aipcb

An AI-native source format for electronics, compiled to KiCad.

The source of truth for a board is a semantic, git-friendly text file describing
*intent*: what the circuit is, what each part is for, what must be true
electrically and physically. KiCad is the backend. It renders, checks, and exports
— it does not own the design.

```
design.yaml  ──►  aipcb build  ──►  .kicad_sch / .kicad_pcb  ──►  kicad-cli  ──►  Gerbers
   ▲                                                                 │
   └──────────────  aipcb check (violations mapped back to source) ◄──┘
```

Source code compiles to a binary; nobody edits the binary. Here the KiCad files are
the binary — except that you *can* open them, and from M6 your manual edits survive
a rebuild.

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
examples/usb-port/design.yaml:47:3: error[asymmetric-diff-pair]: net 'USB_DP'
names 'USB_DM' as its differential partner, but 'USB_DM' names USB_DPP
  at: nets.USB_DM.diff_pair
  hint: set `diff_pair: USB_DP` on 'USB_DM'
```

**Output is deterministic.** Every element's UUID is a hash of its path in the
source, never of generation order or time. The same source produces byte-identical
files, so `git diff` shows only what actually changed — and a violation reported by
`kicad-cli` against a UUID maps straight back to the line that owns it.

**Reads can be partial.** `aipcb query` extracts one module, one net class, or a
one-line status per block, so an agent can look at the part of a design it is
working on without paying for the whole thing in context.

## The layer model

| Layer | What it holds | Where it lives |
|---|---|---|
| 1. Semantic schematic | components, nets, intent, hierarchical parameterised modules | `design.yaml`, [`aipcb.model.design`](src/aipcb/model/design.py) |
| 2. Layout intent | board outline, stackup, placement rules, per-class routing rules | `layout:`, [`aipcb.model.layout`](src/aipcb/model/layout.py) |
| 3. Compiler / sync | elaboration, KiCad emission, check-loop feedback | [`aipcb.elaborate`](src/aipcb/elaborate.py), `aipcb.kicad` |
| 4. Component database | parts, pinouts, limits, KiCad symbol/footprint bindings | `library/*.yaml`, [`aipcb.model.parts`](src/aipcb/model/parts.py) |

No layer stores what a lower one can derive. Layer 1 has no coordinates; layer 2
has intent, not geometry.

## Quickstart

Requires Python 3.11+. KiCad 8 or 9 is needed for the check and export commands;
everything else runs without it.

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

The unconnected items are the ratlines: nothing is routed yet, which is what M7 is
for. Everything else is clean — in particular *schematic parity*, which is KiCad
checking that every footprint is tied to its symbol and every pad sits on the net
the source says it does.

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

`aipcb check` builds the design, runs KiCad's ERC and DRC, and reports what they
found *against the source*:

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
exported 14 files to fab/ (gerbers, drill, position, bom)
```

Gerbers for every copper and technical layer, Excellon drill files with a drill
map, a `.gbrjob`, a grouped BOM carrying the descriptions from your component
database, and a placement file. That is the whole path: source → fab data, with no
step that required opening a GUI.

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

* [`examples/led-blinker`](examples/led-blinker/design.yaml) — an ATtiny85, an ISP
  header, and a parameterised `indicator` module.
* [`examples/ldo-supply`](examples/ldo-supply/design.yaml) — a `regulated_rail`
  module instantiated like a function, with `count:` stamping out bypass caps.
* [`examples/usb-port`](examples/usb-port/design.yaml) — a differential pair with
  an impedance target, skew budget, and routing intent.

## Commands

| Command | What it does |
|---|---|
| `aipcb validate DESIGN` | schema and semantic checks, with source-referenced diagnostics |
| `aipcb build DESIGN` | compile to `.kicad_sch`, `.kicad_pcb`, `.kicad_pro` and the project library tables |
| `aipcb check DESIGN` | build, run KiCad's ERC and DRC, report violations against the source |
| `aipcb export DESIGN` | Gerbers, drill files, BOM and placement file into `out/` |
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
| M7 — topological (rubber-band) routing | next (research) |

## Documentation

* [`docs/format.md`](docs/format.md) — the source-format reference.
* [`docs/decisions/`](docs/decisions/) — architecture decision records. Start with
  [0001 (KiCad I/O)](docs/decisions/0001-kicad-io.md), which explains why this
  project writes its own S-expression layer instead of using `kiutils`.

## Development

```bash
.venv/bin/pytest          # 338 tests, about 2 minutes (it runs KiCad for real)
.venv/bin/ruff check .
.venv/bin/mypy
```

Tests that need KiCad skip with an explicit reason when it is absent, so the suite
runs headless in CI. `AIPCB_FULL_CORPUS=1` runs the lossless round-trip test
against all ~16,000 KiCad files on the machine instead of a 400-file sample.

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
| M5 — query layer for partial reads | next |
| M6 — incremental build preserving manual edits; Gerber export | |
| M7 — topological (rubber-band) routing | research |

## Documentation

* [`docs/format.md`](docs/format.md) — the source-format reference.
* [`docs/decisions/`](docs/decisions/) — architecture decision records. Start with
  [0001 (KiCad I/O)](docs/decisions/0001-kicad-io.md), which explains why this
  project writes its own S-expression layer instead of using `kiutils`.

## Development

```bash
.venv/bin/pytest          # 271 tests
.venv/bin/ruff check .
.venv/bin/mypy
```

Tests that need KiCad skip with an explicit reason when it is absent, so the suite
runs headless in CI. `AIPCB_FULL_CORPUS=1` runs the lossless round-trip test
against all ~16,000 KiCad files on the machine instead of a 400-file sample.

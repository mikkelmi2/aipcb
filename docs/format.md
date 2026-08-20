# The aipcb source format

A design is one YAML file plus the part libraries it names. This document is the
reference; [`examples/`](../examples) holds working designs that exercise
everything described here.

The format is validated by pydantic models, so this document and the schema cannot
drift apart: run `aipcb schema` to emit the JSON Schema for editor completion, and
`aipcb validate` to check a file. Unknown fields are always an error — a misspelled
key must be reported, never silently ignored.

## Contents

- [File structure](#file-structure)
- [Nets](#nets)
- [Components](#components)
- [Modules and instances](#modules-and-instances)
- [Constraints](#constraints)
- [Net classes](#net-classes)
- [Layout intent](#layout-intent)
- [Part libraries](#part-libraries)
- [Elaboration](#elaboration)
- [Diagnostics](#diagnostics)
- [YAML notes](#yaml-notes)

## File structure

```yaml
name: usb-port          # required
revision: A
description: >
  Free text. Worth writing: it is the first thing a reader sees.

libraries:              # part libraries, relative to this file
  - ../library/passives.yaml

net_classes: {}         # routing rules per class     (layer 2)
nets: {}                # electrical nets             (layer 1)
components: {}          # flat components             (layer 1)
modules: {}             # reusable subcircuits        (layer 1)
instances: {}           # module instantiations       (layer 1)
constraints: []         # placement intent            (layer 1)
layout: {}              # board, stackup, placement   (layer 2)
```

Only `name` and at least one of `components` / `instances` are required.

## Nets

A net says what is electrically true, not where copper goes.

```yaml
nets:
  VBUS:
    class: power
    voltage: 5.0
    max_current_a: 0.5
    reason: Bus power. The host may cut it at any time.
  USB_DP:
    class: usb
    impedance_ohm: 90.0
    diff_pair: USB_DM
    description: Connector side of the pair, before the series resistors.
```

| Field | Type | Meaning |
|---|---|---|
| `class` | string | Net class. Built in: `power`, `ground`, `signal`, `analog`, `diff_pair`, `high_speed`, `clock`, `usb`. Any other name must be defined under `net_classes:`. |
| `voltage` | number | Nominal volts. Checked against every connected part's rating. |
| `max_current_a` | number | Informs track width later. |
| `impedance_ohm` | number | Target single-ended or differential impedance. |
| `diff_pair` | net name | Partner net. **Must be declared from both sides.** |
| `description`, `reason` | string | Intent. `reason` is for *why*, `description` for *what*. |

Nets do not have to be declared. A net named only in a component's `pins:` is
created implicitly with class `signal`. Declaring a net is how you attach
attributes to it — and the [dangling-net check](#diagnostics) is what catches the
typo that an implicit net would otherwise hide.

## Components

```yaml
components:
  C1:
    part: C_100N_0603      # required; must resolve in the component database
    role: decoupling
    for: U1
    reason: Local charge reservoir for the MCU's supply pin.
    pins:
      "1": VCC
      "2": GND
```

| Field | Type | Meaning |
|---|---|---|
| `part` | part name | Looked up in the loaded libraries. |
| `pins` | map | Pin **number or functional name** → net name. `VCC: VCC` and `"8": VCC` are the same thing. |
| `role` | identifier | What the component is *for*. See below. |
| `for` | component | The component this one serves. |
| `reason` | string | Why this part, this value, this topology. |
| `value` | string | Overrides the part's default value on the schematic. |
| `refdes` | e.g. `U1` | Explicit reference designator. Usually unnecessary. |
| `dnp` | bool | Do not populate. |
| `count` | int or `{{ param }}` | Stamp out N copies, suffixed `_1` … `_N`. |

### Roles

`role` is what makes the format semantic rather than a netlist with comments.
Checks key off it: a component with role `decoupling` is expected to declare what
it decouples, and placement will later be expected to keep it close.

Known roles are `decoupling`, `bulk`, `bypass`, `pull_up`, `pull_down`, `series`,
`termination`, `snubber`, `current_limit`, `feedback`, `divider`, `filter`,
`crystal_load`, `esd`, `reverse_protection`, `sense`, `load`, `indicator`,
`test_point`, `mcu`, `regulator`, `connector`, `passive`, `power`,
`level_shifter`, `oscillator`.

An unknown role is a warning, not an error: the vocabulary is meant to grow. But
the warning is worth heeding, because a typo'd role silently switches off the
checks that depend on it.

## Modules and instances

A module is a parameterised subcircuit, instantiated like a function call. This is
how the format gets reuse without becoming a programming language — see
[ADR 0002](decisions/0002-source-format.md).

```yaml
modules:
  regulated_rail:
    description: A fixed LDO with its input and output capacitors.
    params:
      regulator:
        type: part
        required: true
      bypass_count:
        type: int
        default: 2
    ports: [VIN, VOUT, GND]
    nets:
      MID: { class: signal }
    components:
      U:
        part: "{{ regulator }}"
        role: regulator
        pins: { VI: VIN, VO: VOUT, GND: GND }
      CBYP:
        part: C_100N_0603
        count: "{{ bypass_count }}"
        role: bypass
        for: U
        pins: { "1": VOUT, "2": GND }
    constraints:
      - kind: max_distance
        between: [CBYP, U]
        mm: 5.0
        reason: Bypass capacitors work through their loop inductance.

instances:
  rail_3v3:
    module: regulated_rail
    reason: The board's only rail.
    params:
      regulator: AMS1117_3V3
    connect:
      VIN: VIN
      VOUT: VOUT
      GND: GND
```

**Parameters** have a `type` (`int`, `float`, `str`, `bool`, `part`, `net`) and
either a `default` or `required: true`. Reference them as `{{ name }}`. A string
that is *only* a reference keeps the parameter's type, so `count: "{{ n }}"` yields
an integer.

**Ports** are the only nets the parent can reach. Every port must be bound in
`connect:`; leaving one out is an error, as is connecting a port that does not
exist.

**Local nets** — anything in `nets:` that is not a port — are scoped to the
instance. Two instances of `regulated_rail` get `rail_a.MID` and `rail_b.MID`, and
cannot short together.

Modules may instantiate other modules. Recursion is caught at 32 levels deep.

## Constraints

Constraints are placement intent. Every one carries a `reason`, because a
constraint whose rationale is lost is a constraint nobody dares to change.

```yaml
constraints:
  - kind: max_distance
    between: [C1, U1]
    mm: 5.0
    reason: A decoupling capacitor works through its loop inductance.

  - kind: keep_apart
    between: [C1, J1]
    mm: 3.0
    reason: The bulk capacitor is tall enough to foul the connector's shell.

  - kind: group
    name: usb_series
    members: [R1, R2]
    reason: The series resistors must sit side by side, or the two halves of the
      pair take different-length detours and the skew budget is spent before
      routing starts.
```

| `kind` | Fields | Meaning |
|---|---|---|
| `max_distance` | `between` (2+), `mm`, `reason` | Members must be within `mm` of each other. |
| `keep_apart` | `between` (2+), `mm`, `reason` | Members must be at least `mm` apart. |
| `group` | `members` (2+), `reason`, `name` | Place as one cluster. |

Constraints inside a module refer to that module's local component names, and are
resolved per instance.

## Net classes

Routing rules, applied per class. This is layer 2: the rules are validated now and
consumed when the board is built.

```yaml
net_classes:
  usb:
    trace_width_mm: 0.25
    clearance_mm: 0.2
    diff_pair_width_mm: 0.34
    diff_pair_gap_mm: 0.2
    impedance_ohm: 90.0
    max_skew_mm: 0.15
    prefer_layers: [F.Cu]
    description: 90 ohm differential on 1.6 mm FR4 with a solid plane below.
```

Also accepted: `via_diameter_mm`, `via_drill_mm` (the drill must be smaller than
the diameter — this is checked).

## Layout intent

```yaml
layout:
  outline:
    shape: rect          # or `polygon`, with `points_mm: [[x, y], …]`
    width_mm: 22.0
    height_mm: 16.0
    corner_radius_mm: 0
  stackup:
    copper_layers: 2     # must be even
    thickness_mm: 1.6
    finish: ENIG
  placement:
    grid_mm: 0.5
    margin_mm: 2.0
    rules:
      - members: [J1]
        side: front
        orientation_deg: 0
        reason: The receptacle overhangs the board edge.
    keepouts:
      - region_mm: [0, 0, 5, 5]
        layers: [F.Cu]
        reason: Mounting hole and its washer.
  origin_mm: [100.0, 100.0]
  routes: []             # topological routing sketches
```

### Routing sketches

`layout.routes` holds route *topology* — which obstacles a route passes and on
which side, never where it is:

```yaml
  routes:
    - net: CROSS
      from: J1.1            # a pad, REFDES.PAD
      to: J2.1
      layer: F.Cu
      passes:
        - obstacle: U1.2    # a pad, a component (`U1`), or a via (`via:v1`)
          side: left        # looking along the direction of travel
          reason: Going over the MCU leaves the lower half of the board for SENSE.
      reason: Routed deliberately; the side it takes decides whether SENSE has a
        corridor.
```

| Field | Meaning |
|---|---|
| `net` | The net this route belongs to. |
| `from`, `to` | The pads it connects. A net with *n* pads needs *n−1* routes. |
| `layer` | The layer it starts on. |
| `passes` | Obstacles passed, in order of travel. Empty means a direct run. |
| `reason` | Why the route goes this way. |

A `passes` entry is either a **pass** (`obstacle` + `side`) or a **via hop**
(`to_layer`, optionally `name`); which one it is follows from the fields present.
Nets with no sketch are routed automatically.

`aipcb route check` verifies each sketch is realizable against the current
placement, and `aipcb route all` builds the copper. The model, the algorithm and
the limits are in [`topology.md`](topology.md).

## Part libraries

A part binds a logical pinout to a KiCad symbol and footprint.

```yaml
parts:
  AMS1117_3V3:
    description: AMS1117-3.3, 1 A fixed 3.3 V LDO regulator, SOT-223
    symbol: Regulator_Linear:AMS1117-3.3
    footprint: Package_TO_SOT_SMD:SOT-223-3_TabPin2
    value: AMS1117-3.3
    keywords: [ldo, regulator, linear]
    pins:
      "1": { type: power_in,  name: GND }
      "2": { type: power_out, name: VO, description: Regulated 3.3 V output }
      "3": { type: power_in,  name: VI }
    limits:
      voltage_max_v: 15.0
      current_max_a: 1.0
    supplier:
      manufacturer: Advanced Monolithic Systems
      mpn: AMS1117-3.3
```

Pin `type` uses KiCad's own vocabulary, because it drives ERC: `input`, `output`,
`bidirectional`, `tri_state`, `passive`, `free`, `unspecified`, `power_in`,
`power_out`, `open_collector`, `open_emitter`, `no_connect`. It defaults to
`passive`.

`limits` are absolute-maximum ratings. `voltage_max_v` is compared against the
`voltage` of every net the part touches — exceeding it is an error, and coming
within 20% of it is a warning.

When KiCad's libraries are installed, `aipcb validate` checks every part in use
against them: that the symbol exists, that the footprint exists, and that the
declared pin numbers match both. Without KiCad, those checks skip with a note.

## Elaboration

Turning the hierarchical source into a flat netlist:

1. **Module expansion.** Instances are stamped out depth-first in sorted order.
2. **Net resolution.** A module net listed in `ports` becomes the parent's net;
   every other becomes `instance.path.NETNAME`.
3. **Reference designators.** Explicit `refdes:` wins. A top-level component whose
   key already looks like a designator (`U1`, `TP12`) keeps it. Everything else is
   numbered per prefix in sorted hierarchical-path order, so the same source always
   produces the same designators.
4. **Pin resolution.** Pin references resolve against the part — by number first,
   then by functional name, case-insensitively.

Every element then gets a UUID that is a hash of its source path. Nothing depends
on generation order or on the clock, which is what makes builds reproducible and
lets violations map back to source. See [`src/aipcb/ids.py`](../src/aipcb/ids.py).

## Diagnostics

Every problem is reported the same way, whether it comes from the schema, the
semantic checks, or (from M4) KiCad itself:

```
examples/usb-port/design.yaml:47:3: error[asymmetric-diff-pair]: net 'USB_DP'
names 'USB_DM' as its differential partner, but 'USB_DM' names USB_DPP
  at: nets.USB_DM.diff_pair
  hint: set `diff_pair: USB_DP` on 'USB_DM'
```

The code in brackets is stable and safe to match on. `--json` produces the same
information with `severity`, `code`, `message`, `location`, `path`, `hint`, and a
`context` object carrying the net or component involved.

Selected checks:

| Code | Severity | Meaning |
|---|---|---|
| `schema-extra-forbidden` | error | Unknown field. Suggests the nearest real one. |
| `unknown-part` | error | No such part in the loaded libraries. |
| `unknown-pin` | error | The part has no such pin; lists the ones it has. |
| `dangling-net` | error | A net with fewer than two connections. |
| `duplicate-pin` | error | One pin connected to two nets. |
| `unconnected-port` | error | A module port left unbound. |
| `voltage-rating-exceeded` | error | A part sits on a net above its rating. |
| `asymmetric-diff-pair` | error | A pair declared from only one side. |
| `unknown-symbol` / `unknown-footprint` | error | The KiCad binding does not resolve. |
| `voltage-derating` | warning | Under 20% margin on a voltage rating. |
| `undriven-power-net` | warning | A power net with no pin that sources it. |
| `role-without-target` | warning | `role: decoupling` with no `for:`. |
| `unknown-role` | warning | A role outside the known vocabulary. |
| `unconnected-pin` | info | A declared pin the design never connects. |

## YAML notes

The loader is deliberately stricter than plain YAML:

* **`yes`, `no`, `on`, `off`, `y`, `n` stay strings.** A net named `NO` must not
  become `False`. Only `true` and `false` produce booleans.
* **Duplicate keys are an error**, rather than the last one silently winning.
* **Mapping keys must be plain text.** Note that pin numbers therefore need
  quoting: `"1": VCC`, not `1: VCC`.
* **Tabs** get an explicit message rather than YAML's cryptic one.

Watch for commas inside flow mappings: `{ description: Reset, also PB5 }` parses as
*two* keys. Quote any value containing a comma.

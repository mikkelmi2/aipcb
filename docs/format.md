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
- [Controlled impedance](#controlled-impedance)
- [The board](#the-board)
- [Mechanical placement](#mechanical-placement)
- [Fanout](#fanout)
- [Copper pours](#copper-pours)
- [Stitching vias](#stitching-vias)
- [Card-edge connectors](#card-edge-connectors)
- [Pair via transitions](#pair-via-transitions)
- [Layout intent](#layout-intent)
- [Signal-integrity simulation](#signal-integrity-simulation)
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
board: {}               # outline, cutouts, edge      (layer 2)
placement: {}           # mechanical placement        (layer 2)
fanout: {}              # escape patterns             (layer 2)
pours: []               # copper pours                (layer 2)
stitching: []           # stitching-via patterns      (layer 2)
layout: {}              # stackup, packing, routes    (layer 2)
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
| `routing` | `auto` \| `manual` | Who lays this net's copper. `manual` keeps aipcb's router off it entirely; the copper comes from a hand route or an external router, and the net is reported as *pending* until it has some. Overrides the class's `routing:` in either direction. |
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

Two roles are *behaviours* rather than descriptions, and both arrived with M11.
`edge_connector` turns on the [card-edge integration](#card-edge-connectors);
`ac_coupling` marks a series capacitor as part of a high-speed pair, and the pair
it sits in is worked out from the capacitor's own nets rather than named again.

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
    layer_forbid: [In1.Cu]
    priority: 90
    rip_up: protected
    description: 90 ohm differential on 1.6 mm FR4 with a solid plane below.
```

Also accepted: `via_diameter_mm`, `via_drill_mm` (the drill must be smaller than
the diameter — this is checked).

### Routing is optional, and declarable

```yaml
net_classes:
  rf:
    trace_width_mm: 0.4
    routing: manual
    description: Drawn by hand against the antenna's reference layout.
```

`routing: manual` declares that this class's copper is not aipcb's to lay. The
router never touches those nets, they are reported as `manual-pending` until copper
appears and `manual-routed` after, and everything else — validation, placement,
pours, ERC, DRC, the high-speed checks — is unaffected. A single net can override
its class in either direction with its own `routing:`.

Three modes fall out of that: full auto (declare nothing), hybrid (declare the
critical nets), and fully manual (declare the lot, or never run the router). See
[`docs/workflows.md`](workflows.md), and
[`docs/external-routers.md`](external-routers.md) for handing the declared-manual
nets to Freerouting headlessly.

### Layers and priority

Four fields tell the router what a class is worth and where it may go.

| Field | Default | Meaning |
|---|---|---|
| `prefer_layers` | all signal layers | Layers this class should use if it can. Naming a **plane** here is how a class opts in to one; it is the only way anything reaches a plane. Other layers stay available at a penalty. |
| `layer_forbid` | none | Layers this class may never use. Outranks `prefer_layers`, and naming a layer in both is an error. |
| `priority` | see below | 0–100. Higher routes first and is harder to push aside. |
| `rip_up` | `normal` | `never`, `protected` or `normal`: how readily the negotiating router may move this class out of a contested corridor. |

An unset `priority` is filled in from the class name — `diff_pair` and `usb` 80,
`high_speed` and `clock` 75, `analog` 65, `power` 60, `ground` 55, everything else
50, and any differential pair 80 whatever its class says. Those are the router's own
ordering heuristic expressed as defaults, so there is one mechanism rather than two.

Priority does two things. It orders the first routing pass, and it decides who keeps
a corridor when two nets want the same one: on each contested cut the net that is
hardest to rip up stays and the rest re-route. `rip_up: protected` multiplies that
resistance by about a dozen ordinary nets; `rip_up: never` means the net is moved
only as the last thing tried before the board is declared unroutable — and if that
happens, the failure report names it. The numbers are in
[`routing-costs.md`](routing-costs.md).

## Controlled impedance

A net class can state an *impedance* instead of a width, and let the stackup work
out the geometry:

```yaml
net_classes:
  pcie:
    trace_width_mm: 0.2          # the single-ended width, for the fan-out
    clearance_mm: 0.15
    diff_pair_gap_mm: 0.15       # an input to the derivation, not an output
    impedance_diff_ohm: 85       # with the stackup, this gives the pair's width
    max_skew_mm: 0.127           # PCIe CEM r3.0 4.7.7, add-in card
    coupling: tight
    standoff_k: 1.4
    reference: In1.Cu
    max_uncoupled_mm: 6.0
    verify: warn
    priority: 95
    rip_up: protected
```

| Field | Meaning |
|---|---|
| `impedance_diff_ohm` | Target differential impedance. Turns on everything else here. |
| `diff_pair_gap_mm` | The gap the pair is built at. The *input* to the width solve: a gap is a manufacturing choice, and solving for width and gap together has no unique answer. Defaults to `clearance_mm`. |
| `diff_pair_width_mm` | An override. The board is built from it, and `aipcb validate` says how far it is from what the target implies once that exceeds 10%. |
| `reference` | The plane this class's return current depends on. Read by the high-speed checks; the router never sees it. |
| `coupling` | `tight` makes `max_uncoupled_mm` a hard budget rather than advice. |
| `max_uncoupled_mm` | How much of each half may run uncoupled: fan-out at the ends, the splay at a via transition, the reach to a coupling capacitor. Exceeding it hands the pair over. |
| `standoff_k` | Tighten against `clearance_mm x k` rather than the bare minimum. Defaults to 3 where an impedance is declared, and is ignored where one is not. |
| `verify` | `warn` (the default) or `error`, for this class's high-speed findings. |

The width comes from IPC-2141's surface-microstrip approximation and the standard
coupling factor, against the dielectric between the class's layer and its
reference. That means the stackup has to be real:

```yaml
layout:
  stackup:
    copper_layers: 4
    thickness_mm: 1.6
    epsilon_r: 4.4
    layers:
      - { name: F.Cu, type: copper, thickness_mm: 0.035 }
      - { name: prepreg_top, type: prepreg, thickness_mm: 0.2104, epsilon_r: 4.4 }
      - { name: In1.Cu, type: copper, thickness_mm: 0.0152 }
      - { name: core, type: core, thickness_mm: 1.065, epsilon_r: 4.6 }
      - { name: In2.Cu, type: copper, thickness_mm: 0.0152 }
      - { name: prepreg_bottom, type: prepreg, thickness_mm: 0.2104, epsilon_r: 4.4 }
      - { name: B.Cu, type: copper, thickness_mm: 0.035 }
```

`layers:` has been in the schema since M1 and was decorative until M11: the
dielectric was assumed to be the leftover board thickness divided evenly. On the
stack above that would be 0.48 mm where the prepreg under `F.Cu` is 0.2104 mm,
which is a 40% error in the derived width. A declared stack is honoured **only if
it is complete** — its copper entries must be exactly the board's copper layers,
in order — because a partial declaration is worse than none.

Without one, the old uniform arithmetic still applies, so every design written
before M11 derives what it always did.

### What is checked, and what is not

`aipcb check` projects every controlled-impedance track onto the plane its class
declares, on the same filled board KiCad's own DRC ran against, and reports every
stretch with no plane under it and every stretch where the plane changes net. It
also reports the width and gap the copper actually holds against the derived
target, the skew after meanders, the uncoupled length against the budget, and each
via's stub computed from the stackup.

**This is rule-based geometry, not electromagnetic simulation.** It says the
return path has somewhere to go, not that it goes there; it says the trace is the
width the arithmetic asked for, not what a field solver would measure. A Gen3
board that passes every check here still wants a human and an SI tool before
anybody signs it off. The `--json` report says the same thing in a `method` field,
on purpose.

## The board

The board's mechanical boundary is its own top-level block, because it is
mechanical *law* rather than intent the compiler may interpret — and because
changing it should be a big diff.

```yaml
board:
  origin: bottom_left        # the source frame's origin. Y is up.
  outline:
    rect: [80, 50]           # shorthand for the common case
    corner_radius: 2         # optional fillet on a rect's corners
    # or the full form:
    # polygon:
    #   - [0, 0]
    #   - [80, 0]
    #   - { arc_to: [85, 5], center: [80, 5] }
    #   - [85, 50]
    #   - [0, 50]
  cutouts:
    - rect: [[70, 20], [78, 30]]
      reason: "flex cable to display"
    - slot: { from: [10, 48], to: [25, 48], width: 2 }
      reason: "cable strain relief"
  edge_clearance: 0.3        # how far copper stays from the edge and the cutouts
```

### The coordinate convention

**The source frame is millimetres, Y up, with the origin at the bottom-left corner
of the outline.** `origin: bottom_left` says so explicitly rather than leaving it to
be inferred, because KiCad's board space is Y *down* and a silent mismatch between
the two is the kind of bug that looks right until the board comes back mirrored.

The emitter converts, once, in one place:

```
kicad_x = origin_x + (x - min_x)
kicad_y = origin_y + (max_y - y)
```

where `origin` is `layout.origin_mm`, which is where the board's top-left corner
sits in KiCad's coordinate space. So a point at the *top* of the board in the source
— larger `y` — comes out at a *smaller* KiCad `y`. Compass directions follow the
source frame: **north is +y**, which is the top of the board as KiCad draws it.

The one place this does *not* apply is the older `layout.placement.rules[].region_mm`,
which predates the board frame and stays in its own: Y down, relative to
`layout.origin_mm`. New designs should use `placement:` below.

### Outlines

`rect: [width, height]` is the shorthand, and is what most boards want. A `polygon:`
is a list of vertices, counter-clockwise, closing implicitly. A vertex is either
`[x, y]` or an arc:

```yaml
- { arc_to: [85, 5], center: [80, 5], direction: ccw }
```

which draws an arc from the *previous* vertex to `arc_to` about `center`.
`direction` defaults to `ccw`, which is the direction that rounds off the corner of
a counter-clockwise polygon. Both ends have to be the same distance from the
centre; `aipcb validate` says so, with both radii, when they are not.

Arcs are emitted to KiCad as arcs — `gr_arc` on `Edge.Cuts` — not as chords.

### Cutouts

A cutout is a hole through the board: `rect: [[x1, y1], [x2, y2]]`, a milled
`slot: { from, to, width }`, or a `polygon:` in the same form as the outline's.

Each one carries a `reason:`, for the same reason a `fixed:` placement does. A
cutout is mechanical law, not reclaimable routing area, and an agent reading the
source has no other way to tell the difference. A cutout with no reason is a
warning.

Cutouts pierce every layer, and the router knows it: a hole is a hole in every
layer's free space, so two points either side of a slot are genuinely in different
homotopy classes and going round it one way is a different route from going round
it the other. `aipcb validate` checks that every cutout lies inside the outline and
that no two of them overlap.

### Edge clearance

`edge_clearance` is how far copper must stay from the board edge and from every
cutout. It feeds two things at once: the router keeps to it, and it is written into
the project as KiCad's `min_copper_edge_clearance` rule, so `kicad-cli pcb drc`
checks the same figure. A design that says nothing gets KiCad's own default,
0.5 mm, from both.

Net-class geometry works the same way. A class asking for something *tighter* than
KiCad's board minimums — a 0.4 mm via for a 0.5 mm-pitch package, say — has those
minimums written into the project too, so DRC checks the board against the rules the
source stated rather than against defaults it never agreed to.

## Mechanical placement

`placement:` is where a component's position comes from outside the electrical
design: a connector aligned to an enclosure opening, a mounting hole on a bolt
circle, a button under a moulded cap, an LED under a light pipe. Coordinates are in
the board frame `board:` defines, so a `placement:` block needs a `board:` block.

```yaml
placement:
  J1:                                # fully fixed: mechanical law
    fixed: { x: 0, y: 15, rot: 90, side: front }
    reason: "enclosure port opening, see mech/enclosure-v3.step"
  H1:
    fixed: { x: 3.5, y: 3.5 }
    role: mounting_hole
  SW1:                               # partially constrained
    edge: { side: north, offset_range: [20, 40], rot: 0 }
  D5:
    region: { rect: [[10, 10], [30, 25]] }   # anywhere inside this area
  # everything else: the relative intents under `constraints:` and `layout:`
```

Three levels, and they outrank each other in this order:

| Level | Means |
|---|---|
| `fixed` | An exact position and rotation. The placer never moves it. |
| `edge` / `region` | The placer chooses, but only from the set the source allows. |
| relative intent | `constraints:` and `layout.placement`. Groups, proximity, keep-apart. |

A relative intent that names a fixed part constrains only the *other* parts: the
group deforms around the anchor, and the anchor does not move. A cluster that
contains an anchor is packed *around* it, which is how a decoupling group follows
its connector to the board edge without anything new being said.

`fixed` coordinates are the footprint's own origin — the point KiCad stores as its
position — and are **not** snapped to `layout.placement.grid_mm`. A grid is a
convenience for parts nobody cares about the exact position of; a connector aligned
to an enclosure is not one of them.

`edge` sides are `north`, `south`, `east`, `west` in the source frame, and
`offset_range` is how far along that edge the part may sit — measured in `x` for
north and south, in `y` for east and west. On a board that is not a rectangle, the
edge is found from the real outline at the chosen offset, so a part on the north
edge of an L-shaped board sits against the boundary that is actually above it.

`side: back` on a `fixed` placement still validates, warns, and places on the
front. Mirroring a footprint is deferred rather than approximated — see
[`roadmap.md`](roadmap.md).

`reason:` is free text and strongly encouraged on `fixed`: it is what tells the next
reader — or the next agent — that the part *cannot* move, and where the authority
for that lives. A `fixed` placement with neither `reason:` nor `role:` is a warning.

### Conflicts, caught before anything is built

`aipcb validate` checks the mechanical model against geometry alone, with no build:

| Code | Severity | Means |
|---|---|---|
| `fixed-courtyards-overlap` | error | Two fixed parts want the same board area. |
| `fixed-part-outside-outline` | error | A fixed part falls outside the real polygon — not its bounding box. |
| `part-over-cutout` | error | A courtyard covers a hole there is no board in. |
| `placement-set-empty` | error | An `edge` or `region` allows no position that fits. |
| `cutout-outside-outline` | error | A hole reaches past the board edge. |
| `cutouts-overlap` | error | Two holes share area, so they are one hole. |
| `board-arc-inconsistent` | error | An arc's two ends are different distances from its centre. |
| `board-outline-self-intersecting` | error | The outline crosses itself. |
| `constraint-unreachable` | warning | A `max_distance` the anchors already make impossible. |
| `fixed-placement-without-reason` | warning | A position nobody can tell is law. |
| `cutout-without-reason` | warning | A hole nobody can tell is law. |

The last is deliberately conservative. `constraint-unreachable` reasons with
intervals — the closest two allowed sets can possibly be — and speaks only when that
lower bound already exceeds what the constraint asks for. A complaint means the
constraint really cannot hold; silence means nothing either way.

### When somebody moves a part in KiCad

A `fixed:` placement is mechanical law, so `aipcb build` puts a hand-moved part back
where the source says — and reports that it did, as `fixed-placement-drift`. Movable
parts keep M6's behaviour unchanged: the source never said where they go, so the
human's position stands.

When the board is right and the YAML is stale, `aipcb sync-placement` goes the other
way:

```console
$ aipcb sync-placement examples/enclosure/design.yaml
J1 moved 0.447 mm from its fixed position in source: (4, 17) -> (4.4, 16.8)

1 part moved. Re-run with --apply to write these positions into the source, or
run `aipcb build` to put them back where the source says.

$ aipcb sync-placement examples/enclosure/design.yaml --apply
```

The edit is surgical: the `fixed:` line is rewritten and every other line of the
file — comments, `reason:`, the rest of the design — is left exactly as it was. An
`edge` or `region` entry becomes a `fixed` one, because moving the part by hand is
what that means. `--json` lists the drift without asking anything.

## Fanout

A fine-pitch package is where rubber-band routing meets its density ceiling. A
QFN-32 on a 0.5 mm pitch leaves about 0.25 mm between neighbouring pad edges, and a
0.25 mm track with 0.2 mm clearance needs 0.65 mm of corridor: there is nothing to
get out through.

Escaping one is not a routing problem, it is a *pattern*, and `fanout:` asks for it:

```yaml
fanout:
  U1:
    style: auto          # auto | dogbone | via_in_pad | none
    escape_layers: [In1.Cu, B.Cu]
    via: { drill: 0.2, diameter: 0.45 }   # defaults from the net class if omitted
    reason: Nothing escapes a 0.5 mm pitch on the layer the part is on.
```

`aipcb route all` runs the generators first, then routes between the escape
terminals. The generator lays a short stub from each pad to a via just clear of the
part, registers that copper as a fixed obstacle, and publishes the via as the
terminal the router sees *in place of* the package pad. The router that runs
afterwards has no idea a fanout happened.

| `style` | What it lays |
|---|---|
| `auto` | Looks at the geometry: more than one interior pad means an area array, which gets dog-bones; anything else gets perimeter stubs. |
| `dogbone` | A via in the gap the pad's quadrant points toward — the classic BGA escape. |
| `via_in_pad` | A via at the pad centre. Never chosen for you: under a solder ball it needs filling and capping, and costs real money. |
| `none` | Nothing. The router reaches the pads itself, or says it cannot. |

Details that matter:

* **The barrel reaches every layer named.** `escape_layers` is where the signal may
  be picked up; the via spans from the package's own layer to the deepest of them,
  so the router can take whichever corridor it likes.
* **Unused pads get no fanout.** A pad the design never connects has nothing to
  escape to.
* **Neighbouring escapes stagger into two rows**, decided by where the pad is rather
  than by where it comes in a list. At 0.5 mm pitch a single row of 0.45 mm vias
  would be one continuous piece of copper.
* **An interior pad on a perimeter package is a thermal pad** and gets a via straight
  through it. That is not the expensive via-in-pad case: what costs money is a filled
  and capped via under a solder ball, and an exposed pad's thermal vias are neither.
* **A power or ground pad may get several vias**, in a short column along the escape.
  The model is that one via carries about as much current as a track as wide as its
  barrel, so a net whose class asks for a fat track asks for proportionally more
  vias. It is a rule of thumb, and it is stated rather than hidden.
* **Everything is keyed by pad instance**, never by pad number: a QFN's exposed pad
  and its pin 1 are both real copper and only one of them is called "1".
* **The escape has to fit.** The generator respects the package's own pads, its
  courtyard, the board outline and every cutout, and where a pad has nowhere to go it
  says so (`fanout-pad-not-escaped`) and leaves that pad to the router rather than
  laying copper DRC will reject.
* **Escapes the router did not use come back out.** The generator has to propose one
  per pad before anything is routed; where the router reached a pad without ever
  using the far layer, the via would join copper to nothing, so it is removed.

## Copper pours

Nearly every real board wants the leftover copper on its outer layers given to
ground, and many want a plane split between two supplies. `pours:` says which net
owns which copper, and under what rules:

```yaml
pours:
  - net: GND
    layers: [F.Cu, B.Cu]     # or `layer: In1.Cu` for one
    scope: board             # the whole board, minus cutouts and edge clearance
    priority: 0
    connect: thermal         # thermal | solid  (default thermal)
    reason: The return path everything above it needs.

  - net: VDD_3V3
    layer: In2.Cu
    region:                  # a split plane: this rectangle, not the whole layer
      rect: [[10, 10], [60, 40]]
    priority: 1              # higher priority keeps the copper where zones overlap
    clearance: 0.3           # to copper of other nets; defaults to the net class
    min_width: 0.25          # thinnest sliver of poured copper to keep
    remove_islands: always   # always | never | below_area
    min_contiguous: 0.7      # warn if the plane comes back in pieces
```

| Field | Meaning |
|---|---|
| `net` | The net the copper belongs to. Must exist. |
| `layer` / `layers` | One copper layer, or several. Must be in the stackup. |
| `scope` | `board` pours everything. Mutually exclusive with `region:`; omitting both means `board`. |
| `region` | A `rect: [[x1, y1], [x2, y2]]` or a `polygon:` of vertices, in the board frame — the same Y-up frame `placement:` uses. |
| `priority` | Where two zones overlap on one layer, the higher priority is poured first and keeps the copper. Equal priorities over an overlap are an **error**: which one won would depend on file order. |
| `connect` | How the zone attaches to pads of its own net. `thermal` gives relief spokes; `solid` floods. |
| `pad_connect` | Per-pad-instance overrides of `connect`. See below. |
| `clearance`, `min_width`, `thermal_gap`, `thermal_bridge_width` | KiCad's zone parameters. Defaults come from the net class and from the note on thermal relief below. |
| `remove_islands` | What to do with poured copper that reaches no pad. `below_area` needs `island_area_min` in mm². |
| `min_contiguous` | Fragmentation threshold, 0–1. See *plane integrity*. |
| `hatch` | KiCad's hatched-fill parameters, passed through unchanged. |
| `name`, `reason` | A label KiCad shows, and why the pour exists. |

A pour respects the board it is on: KiCad clips it to the outline and to every
cutout, less the edge clearance, without being told twice. It also respects
`layout.placement.keepouts` — each one is emitted as a KiCad keepout zone that
excludes copper pour, because the router has always honoured those and the *filler*
had no way to know about them. Keepout zones are emitted only for a design that
declares a pour; without one there is nothing to keep out that the router does not
already handle.

### Who fills the copper

**KiCad does.** `aipcb` emits the zone — its boundary and its rules — and never a
single filled polygon. Reimplementing zone fill is deliberately rejected: KiCad's
fill is what DRC checks against, so a second implementation would be checked
against the first, would differ, and the difference would be a bug on every board
([ADR 0009](decisions/0009-pours.md)).

`kicad-cli` 9.0.8 has no way to fill a zone, measured rather than assumed, so
`aipcb` drives KiCad's own filler through a `pcbnew` subprocess. That needs KiCad's
Python module — the `kicad` package rather than only `kicad-cli` — and **only for
designs that declare `pours:`**. A design without them never invokes it. The
subprocess checks that `pcbnew` and `kicad-cli` are the same KiCad version and
stops if they are not, because the whole point of using KiCad's filler is that it
is the same engine DRC checks against. Set `AIPCB_PCBNEW_PYTHON` to point at the
interpreter that can `import pcbnew` if the default search does not find it.

### The stability policy

**The byte-identical guarantee covers build output.** `aipcb build` is a pure
function of the source: the same design produces the same `.kicad_pcb`, byte for
byte, with its zones **unfilled**. That is the file `git diff` should be readable
on, and the file every earlier guarantee in this document is about.

**Fill is a derived artefact**, regenerated at check and export time into a staged
copy, so build output stays the unfilled reference. It was measured to be
deterministic — filling one board five times in five separate processes produced
byte-identical fill geometry every time — but the *filled file* is not byte-stable
and is not promised to be: KiCad's writer adds empty `Datasheet` and `Description`
properties to footprints that lack them, each with a freshly random UUID, so twelve
lines of a filled `usb-port` differ between two runs that produced identical
copper. Nothing downstream depends on those bytes; everything downstream depends on
the copper, and the copper is stable.

### Thermal relief, and per-pad overrides

The default is thermal relief, because a plane that floods every pad is a plane
nobody can hand-solder to. The relief `aipcb` writes is a 0.25 mm gap with a 0.5 mm
bridge, **not** KiCad's dialog default of 0.5 mm for both: that default was measured
to produce boards KiCad's own DRC rejects, because on a 1.7 mm through-hole pad at
2.54 mm pitch only one of the four spokes can reach the plane and KiCad 9 wants at
least two. A pour that wants KiCad's figures says so with `thermal_gap:` and
`thermal_bridge_width:`.

One pad usually wants the opposite. A QFN's exposed pad or a receptacle's shield tab
exists to move heat or current into the plane, and relief spokes there are a thermal
decision made by accident:

```yaml
    pad_connect:
      - pads: [J1.6#7]
        connect: solid          # solid | thermal | none
        reason: A shield tab wants the lowest-impedance path to ground it can get.
```

`J1.6#7` names one **pad instance**: the seventh pad numbered 6 in the footprint's
own pad order, which is the same key the router uses for its obstacles. That matters
more than it sounds — a Micro-B receptacle has twelve pads numbered 6, and a
SOT-223's tab *is* pin 2, a second pad carrying the same number, so a pad number is
not an identity.

Both forms exist because both are needed:

| Reference | Which pads |
|---|---|
| `U2.4` | **every** pad numbered 4 on `U2` — how `examples/enclosure` floods all twelve of a receptacle's shield tabs in one line |
| `U2.4#2` | the **second** pad numbered 4, and no other — how `examples/usb-port` floods one tab and leaves eleven thermal |

The suffixed form wins where both name the same pad.

### Plane integrity

After the fill, `aipcb check` reads the filled polygons back and reports what the
copper actually came out as — per pour, per layer: how many disconnected islands,
how much of the copper is in the largest one, how much of the pour's scope it
covers, and the bounding box of each island. It is feedback, not a gate; the
numbers are in `aipcb check --json` under `summary.planes` and in the text output as
`plane-integrity` notes.

Two things it will tell you unprompted:

* **island removal deleted copper** — the pour's outline suggests more plane than
  the board has, because pieces that reached no pad were dropped;
* **fragmentation past `min_contiguous`** — a *warning*, never an error, pointing at
  the pour's own line. Fragmented-but-functional is common, and only the designer
  knows whether this plane is. `examples/qfn-fanout` ships with the warning firing
  on purpose: a 0.5 mm-pitch escape field really does cut the back plane into
  pieces.

## Stitching vias

Two pours on two layers are two sheets of copper until something joins them.
`stitching:` generates the vias that join them — a pattern, never a route:

```yaml
stitching:
  - net: GND
    between: [F.Cu, B.Cu]    # defaults to the outer pair
    pattern: grid            # grid | edge | ring
    pitch: 5.0
    via: { drill: 0.3, diameter: 0.6 }   # defaults from the net class

  - net: GND
    pattern: edge            # a row following the board outline
    pitch: 3.0
    inset: 1.0

  - net: GND
    pattern: ring            # a fence around a noise source
    around: U3
    pitch: 2.0
    radius: 6.0              # defaults to just clear of the part
```

| `pattern` | Where the vias go |
|---|---|
| `grid` | A lattice over the area the net's pours share on both layers. Anchored to multiples of the pitch in board coordinates, so two patterns at one pitch interlock. |
| `edge` | A row following the outline polygon — arcs included — at `inset` millimetres inside it. |
| `ring` | A circle around the component named by `around:`, or around a `region:`. |

Every candidate has to sit inside the net's pours on **both** layers it joins. That
is not fussiness: a stitching via outside the pour is an isolated piece of copper,
which KiCad reports as an unconnected item. Candidates that would break clearance to
a track, a pad, another hole, a cutout or the board edge are dropped **silently** —
that is what a pattern generator is for — but the counts come back in the check
report (`stitching: 69 vias placed, 17 positions skipped`) and under
`summary.stitching` in `--json`.

Stitching runs after routing and before the fill, and its output is ordinary vias:
obstacles to any later routing run, preserved like everything else, and given
derived UUIDs so a second run replaces its own work rather than piling more on top.

## Card-edge connectors

Gold fingers are a footprint, not a generator. KiCad ships them —
`Connector_PCBEdge:BUS_PCIexpress_x1` and its wider relatives — and what a design
has to do is *integrate* one:

```yaml
components:
  J_PCIE:
    part: PCIE_X1_EDGE
    role: edge_connector
placement:
  J_PCIE:
    fixed: { x: 24.5, y: 3.45, rot: 0 }
    pour_keepout_mm: 0.6
    reason: the fingers have to coincide with the card edge
```

`role: edge_connector` turns on four behaviours:

* **the placement must be `fixed`.** A card edge is mechanical law, not a
  preference, so the placer must not be free to move it.
* **the outline has to agree with the footprint.** A card-edge footprint draws its
  own `Edge.Cuts`: the tongue, the chamfered leading edge, the keying notch.
  aipcb does **not** emit that geometry — an outline with two authors is an
  outline waiting to disagree, and KiCad reports the result as a self-intersecting
  board — so the `board:` block reproduces it and validation checks the two match
  to within 0.01 mm. Where they do not, the error hands the missing vertices back
  in the source's own frame, ready to paste.
* **the finger field gets a pour keepout**, on the outer layers only:
  `pour_keepout_mm` on the placement entry, half a millimetre by default. Plating
  and pour copper must not meet at the card edge; an inner plane under the fingers
  is not plated and is the reference the pairs entering them are designed against.
* **the board thickness is checked against the slot**, and a note goes into the
  report for the leading-edge bevel, measured from the footprint: it is a process
  step rather than geometry any Gerber can carry, and a card that arrives without
  it does not go into a slot.

Nets connect to the footprint's pads the ordinary way. There is no special pad-map
syntax and there does not need to be.

**One consequence, stated because it is visible.** Because the footprint is placed
without its own `Edge.Cuts`, KiCad's `lib_footprint_mismatch` rule reports that it
is not byte-identical to its library copy. It is right, and there is no way to
have both. See [ADR 0010](decisions/0010-highspeed.md).

## Pair via transitions

A differential pair changing layer is a pattern, not two vias, and the source
names where it happens:

```yaml
transitions:
  - pair: [PCIE_RXP, PCIE_RXN]
    at: [42.4, 20.0]
    between: [F.Cu, B.Cu]
    return_vias: 2
    return_within_mm: 1.2
    return_net: GND
    via: { drill: 0.2, diameter: 0.4 }
    reason: the receive pair arrives on the A-side fingers, which are on the back
```

The generator lays two signal vias symmetric about `at`, on the line
perpendicular to the pair's direction of travel, and the return vias on that same
line — the only direction the pair does not occupy, since it arrives along the
travel axis on one layer and leaves along it on the other. It then publishes two
coupled segments, one per layer, and the router routes them as pairs in the
ordinary way.

**The column is wider than the pair.** Two 0.4 mm vias at a 0.44 mm pair pitch
would leave 0.04 mm of laminate between two nets that want 0.15 mm, so the column
opens out to `via diameter + clearance` and the halves splay to reach it. That
splay is uncoupled length and is counted against `max_uncoupled_mm` like any
other: the discontinuity is real, and the point is that it is measured rather than
avoided by refusing the pair.

`return_within_mm` is a limit, not a target. A return via that cannot sit inside
it is not placed, and the report says two-of-two or one-of-two rather than
claiming both. Per transition it also reports the **stub**: the barrel left below
the layers the signal uses, computed from the stackup. An outer-to-outer through
via has none; a transition that stops on an inner layer abandons the rest.

Where a pair transitions, `aipcb check` measures the whole conductor's skew across
both segments rather than each segment's own.

## Layout intent

Everything the compiler is free to interpret: how thick the stack is, how tightly
to pack, what may not be entered, and which routes were sketched by hand.

```yaml
layout:
  stackup:
    copper_layers: 4     # must be even
    thickness_mm: 1.6
    finish: ENIG
    planes:              # copper layers given over to a plane
      - layer: In1.Cu
        net: GND
        reason: The reference the pair on F.Cu is designed against.
    via_types: [through] # or blind, buried — through only, by default
    preferred_direction: # a soft H/V hint per layer
      F.Cu: horizontal
      B.Cu: vertical
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
        layers: [F.Cu]     # omit for every layer
        reason: Mounting hole and its washer.
  origin_mm: [100.0, 100.0]
  routes: []             # topological routing sketches
```

`layout.origin_mm` is where the board's top-left corner sits in KiCad's coordinate
space. Nothing about a design depends on it; it exists so the generated files open
somewhere sensible on an A4 sheet.

### The older outline block

`layout.outline` still works and still means what it always meant:

```yaml
layout:
  outline:
    shape: rect          # or `polygon`, with `points_mm: [[x, y], …]`
    width_mm: 22.0
    height_mm: 16.0
    corner_radius_mm: 0
```

Its coordinates are Y *down*, relative to `layout.origin_mm`, and it cannot express
arcs or cutouts. Declaring both it and [`board:`](#the-board) is an error rather
than a silent precedence rule. New designs should use `board:`; migrating is one
block, and for a rectangle the generated files come out byte for byte identical.

`layout.placement.rules[].region_mm` is in that same older frame. The
[`placement:`](#mechanical-placement) block is the supported way to say where
something goes.

A keepout is a region nothing may enter, given relative to the board origin like
every other region. The router honours it: no track, no via barrel, on the layers it
names or on all of them when it names none. `examples/congestion` uses a pair of
them to cut the board down to a single channel, which is what makes that board
unroutable on one layer. A design that also declares [`pours:`](#copper-pours) gets
each keepout emitted as a KiCad keepout zone as well, so the fill stays out of it
too.

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
placement *and* that the declared routes fit alongside each other — every corridor
across the free space has a capacity, and a set of routes that over-subscribes one
cannot be built however sound each route is on its own. `aipcb route all` builds the
copper, across every signal layer the stackup allows. The model, the algorithm and
the limits are in [`topology.md`](topology.md); what the router is minimising is in
[`routing-costs.md`](routing-costs.md).

### Layers, planes and vias

A layer listed under `stackup.planes` is closed to signal routing. That is enforced,
not advisory: nothing is placed there, and the nets that would have lived on it are
routed as tracks on the signal layers. A net class reaches a plane only by naming it
in `prefer_layers`. Pouring the plane is not built yet, so today a declared plane is
a reservation.

`via_types` says which spans the fabricator will build. The default is through-only,
because blind and buried vias are a real extra cost that nobody should discover in a
quote; a stackup that lists `blind` or `buried` gets them where they help. A via is
modelled as a *column* — copper on every layer its barrel passes, not only the two
it connects — so an inner-layer track never runs through a drill.

`preferred_direction` is the classic H/V hint. It is soft: going against the grain
costs 25% more per millimetre, which biases without producing staircases.

## Signal-integrity simulation

`aipcb simulate` solves each differential pair with openEMS and reports what it
measures against what the net class declared. It is a separate command, not part of
`check`: a pair costs a minute or two, and what comes back is engineering judgement
rather than a correctness gate.

Everything it needs is derived from the design already — the pairs from `diff_pair:`,
the stackup from `layout.stackup`, the impedance target from `impedance_diff_ohm`,
and which series parts are shorts from `role: ac_coupling`. The optional
`simulation:` block exists for the handful of numbers the board itself does not
carry:

```yaml
simulation:
  stop_ghz: 8.0            # top of the swept band
  start_mhz: 100.0
  margin_mm: 1.5           # how much board to keep around the pair
  launch_mm: 1.5           # the straight run each port feeds through
  grid_optimal_um: 50.0    # target mesh cell size near copper
  grid_inter_layers: 8     # mesh cells through each dielectric
  max_steps: 40000
  timeout_s: 7200          # wall clock one pair may take before it is killed
  impedance_tolerance: 0.10   # +/- this against the class target still passes
  return_loss_db: -10.0
  insertion_loss_db: -3.0
  mode_conversion_db: -20.0   # differential turned into common mode; skew does this
  classes:
    sata:
      stop_ghz: 6.0
      reason: SATA III is 6 Gbit/s, so the third harmonic is where the eye stops
        caring.
```

Every field is optional and every per-class field falls back to the global one. A
design that omits the block entirely simulates on the defaults above.

**The thresholds are engineering defaults, not standards compliance.** Ten percent
on impedance and −10 dB of return loss are what most controlled-impedance
fabrication notes ask for; a link with a real budget should state its own. Nothing
here certifies a design against PCIe or SATA.

### What a run produces

One directory per pair under `out/si/`, each holding the *slice* — a small,
self-contained `.kicad_pcb` carrying that pair, the copper near it, the planes it is
referenced to and four simulation ports — its Gerbers, the solver's inputs and
outputs, and the raw S-parameters as a Touchstone `.s4p` for scikit-rf or anything
else. A `manifest.json` beside them records what ran, what was reused from cache and
how long each pair took.

Slices are deterministic: the same routed board produces byte-identical slices. The
solver's own numerics are not bit-reproducible, but they are stable enough that the
pass/warn verdicts repeat.

### What it cannot tell you

The stackup it solves is the one the *source* declares. A fabricator who presses
different material builds a different impedance, and no simulation here will know.
This validates the layout; it does not replace an impedance coupon or a hardware
measurement.

**`timeout_s` is a budget, not an estimate.** A run that hits it is killed and
writes nothing at all, so the next run re-slices and starts from zero — which makes
a timeout set near the typical run far more expensive than one set well above it.
Size it above the slowest link on the board: on `examples/pcie-sata` the slowest
measured is 3 216 s, and the design writes 7200.

Five approximations are deliberate and worth knowing about:

* Each end of a pair grows a short straight **launch**, because a microstrip port
  has to run along an axis and a trace that ends on a diagonal has nowhere to be fed
  from. The port occupies exactly that launch, so it does not lengthen the link — but
  the pad and antipad that were really there are not modelled. The launch runs
  perpendicular to the pair's own separation, so the two halves stay parallel rather
  than overlapping.
* **The corridor the launches occupy is cleared** of other nets' copper. A launch
  runs outward from where the pair ends, which is where a pad was, and past the pad
  is the component — with the footprints gone, that region is full of the next pins'
  fanout. The port stands in for the pad and for the driver, connector and cable
  beyond it, so the fanout is removed rather than shorted to. Each slice reports how
  much copper it took out.
* **Footprints are dropped** from the slice. The interior of a run, which is what an
  impedance target is about, is unaffected; the last few tenths of a millimetre at
  each end are not.
* Series parts marked `role: ac_coupling` become **copper bridges**, which is what
  they are at signal frequencies. This is the part a geometric slicer cannot do,
  because only the source knows the capacitor is not a component under test. The
  bridge is the same width and on the same layer as the pads it lands on, and it
  carries their nets — a bridge with no net is copper the mesh generator is never
  told to resolve, and it would sit exactly on the discontinuity a run is trying
  to measure.
* **The slice's return path is made a single conductor.** Every layer the stackup
  declares a `plane` is tied to the reference net, and a ring of reference-net vias
  is added just inside the slice outline. Both are copper the board does not have,
  and both are there because the cut removes what makes the board's return path
  continuous: a supply plane is a reference *because* it is decoupled to ground, and
  the slice keeps neither the decoupling nor the pads nor the supply, while the
  pours themselves stop in mid-air where the board carries them on and stitches them.
  Modelled without either, the planes are isolated plates and the slice is a
  resonator: on this board `REFCLK` held a tenth of its energy for forty thousand
  timesteps, and with both it decays past −39 dB. What it costs is the ability to
  tell you that a supply plane is *badly* decoupled, which this was never able to
  measure anyway.

Two of them are checked rather than trusted, on every slice, before the solver is
started:

* **Every sheet of copper in the slice is bonded to the others**, and a slice that
  leaves one floating is refused by name — the layer, the net and the square
  millimetres at no defined potential. A zone declared on three layers is three
  sheets until a via joins them, which is the form the defect actually took: one
  `examples/pcie-sata` slice reached the solver with two pours and both planes
  mutually isolated, under a 100 Ω microstrip, and reported nothing.
* **The plane a controlled-impedance class names as its `reference:` is one of
  them.** An impedance number is a statement about a line *and* its return path, so
  a slice that does not connect the declared plane is not measuring the class it
  says it is.

Both are checked before the slice is filled, so what they see is a zone's outline
rather than its poured copper. That answers "is this sheet tied to anything" and not
"is this sheet in one piece" — the second is what `check`'s plane-integrity report
measures, on the real board, where the fill exists.

### Running several at once

`aipcb simulate --parallel N` (`-j N`) solves N pairs at a time, each solver pinned
to its own cores where the host allows that. `-j 0` picks one per five cores. The
default is 1.

**Measure before you use it.** openEMS does not use the whole machine — its
multithreaded engine benchmarks itself at startup and settles on four to six threads
whatever it is given — but on the machine this was developed on, running three
solvers at once came back **48 % slower** than running them one after another, and
their combined throughput was lower than a single solver's. What runs out is memory
bandwidth, which is also why the engine declines the spare cores; a second process
does not create any. [ADR 0011](decisions/0011-si-simulation.md) Decision 4a has the
numbers. A host with more memory channels may well go the other way, which is why
the flag exists.

Two things it does not change, both checked: the results, which agree with a
sequential run to 0.04 %, and the order of the report, which is by pair rather than
by whoever finished first — a concurrent batch produces the same `manifest.json` a
sequential one does. Each run reports the throughput and thread count the solver
chose, so a batch can be compared against another machine's, and it records whether
it was able to pin: rootless podman on cgroup v2 is not given the `cpuset`
controller, so concurrent runs there share every core instead of dividing them.

Running it needs a container runtime and a locally built gerber2ems image; see
[ADR 0011](decisions/0011-si-simulation.md), which records the pinned commits. It is
the only part of aipcb that needs either.

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
| `route-unrealizable` | error | A declared sketch cannot be built on this placement. |
| `route-cut-over-subscribed` | error | Declared routes need more of a corridor than it has. |
| `fixed-courtyards-overlap` | error | Two fixed parts want the same board area. |
| `fixed-part-outside-outline` | error | A fixed part falls outside the real outline polygon. |
| `part-over-cutout` | error | A courtyard covers a hole there is no board in. |
| `placement-set-empty` | error | An `edge` or `region` allows no position that fits. |
| `cutout-outside-outline` | error | A cutout reaches past the board edge. |
| `cutouts-overlap` | error | Two cutouts share area, so they are one hole. |
| `board-arc-inconsistent` | error | An arc's ends are different distances from its centre. |
| `board-outline-self-intersecting` | error | The outline crosses itself. |
| `unknown-mechanical-member` | error | `placement:` or `fanout:` names no such component. |
| `voltage-derating` | warning | Under 20% margin on a voltage rating. |
| `undriven-power-net` | warning | A power net with no pin that sources it. |
| `role-without-target` | warning | `role: decoupling` with no `for:`. |
| `unknown-role` | warning | A role outside the known vocabulary. |
| `route-handed-over` | warning | A connection the router refused, with its `unrouted:` category and the corridor that blocked it. |
| `constraint-unreachable` | warning | A `max_distance` the mechanical anchors already make impossible. |
| `fixed-placement-drift` | warning | A `fixed` part was moved in KiCad and has been put back. |
| `fixed-placement-without-reason` | warning | A fixed position with nothing saying it is law. |
| `cutout-without-reason` | warning | A hole with nothing saying it is law. |
| `fanout-pad-not-escaped` | warning | A pad the escape pattern could not clear; left to the router. |
| `fanout-via-in-pad` | warning | Via-in-pad was asked for, and costs money at the fabricator. |
| `diff-pair-not-coupled` | warning | A pair was routed as two ordinary nets, with the measurement that decided it. |
| `diff-pair-skew` | warning | A pair misses its `max_skew_mm` and could not be meandered into it. |
| `diff-pair-wall-hugging` | warning | A controlled-impedance run stayed within 3x its gap of another copper feature for longer than 5x it, and re-tightening did not move it. |
| `impedance-geometry-override` | warning | An explicit width more than 10% from what the impedance target implies, with the impedance it will actually produce. |
| `impedance-unreachable` | warning | The target is outside what this gap and stackup can reach. |
| `impedance-reference-missing` | error | `reference:` names a layer the board does not have. |
| `impedance-reference-is-signal-layer` | error | A pair cannot be its own return path. |
| `impedance-reference-not-a-plane` | warning | The reference layer is not declared a plane, so the router may put signals on it. |
| `impedance-reference-not-poured` | warning | A reserved layer with no copper on it is not a reference. |
| `impedance-no-uncoupled-budget` | warning | `coupling: tight` with no `max_uncoupled_mm` to enforce. |
| `impedance-rules-inert` | warning | High-speed fields on a class with no `impedance_diff_ohm`. |
| `edge-connector-notch-missing` | error | The outline does not reproduce the footprint's edge geometry. The hint carries the vertices. |
| `edge-connector-off-edge` | error | None of the footprint's edge geometry is on the board boundary. |
| `edge-connector-not-fixed` | error | A card edge without a `fixed:` placement. |
| `edge-connector-thickness` | warning | The board is not the thickness the slot expects. |
| `ac-coupling-asymmetric` | warning | The two coupling capacitors are out of line along the route, which is skew built into the placement. |
| `ac-coupling-unpaired` | warning | One capacitor marked `ac_coupling` with no partner in the other half. |
| `transition-unknown-net` / `transition-not-a-pair` / `transition-unknown-layer` | error | A `transitions:` entry that does not describe anything on this board. |
| `transition-return-vias` | warning | Fewer return vias than asked for fitted inside `return_within_mm`. |
| `hs-reference-broken` | warning | A controlled-impedance track crosses a void or a net change in its declared reference plane, with the length and the position. |
| `hs-width-deviation` / `hs-gap-deviation` | warning | The copper is more than 10% from the impedance-derived geometry. |
| `hs-skew` | warning | A pair misses its budget after meanders, measured across every segment. |
| `hs-via-stub` | warning | A through via leaves more stub than the threshold, with the barrel and the layers the signal used. |
| `edge-connector-outline-matches` | info | The outline reproduces the footprint's edge geometry. |
| `edge-connector-fab-note` | info | The finger set-back, and what the fabricator has to be told about plating and the bevel. |
| `ac-coupling` | info | The two capacitors, the pairs they join, and how level they are. |
| `transition-generated` | info | How many transitions were laid, the return vias placed, and the worst stub. |
| `hs-reference-continuous` | info | How much controlled-impedance track was projected, and that nothing broke under it. |
| `hs-pair-geometry` / `hs-coupling` / `hs-via-stubs` | info | The measurements, whether or not anything is wrong. |
| `diff-pair-coupled` | info | A pair was routed as a pair, with its gap and fan-out. |
| `diff-pair-length-matched` | info | Meander added to close a pair's skew. |
| `route-repaired` | info | A connection was re-routed against the board as it stood. |
| `routing-congested` | info | The negotiation did not settle. What it could not then realize legally is handed over. |
| `fanout-generated` | info | How many escapes were laid, and for which packages. |
| `fanout-escapes-settled` | info | Escapes the router turned out not to need, taken back out. |
| `vias-merged` | info | Two barrels of one net through the same copper, drilled once. |
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

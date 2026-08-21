# Roadmap: what is deliberately not built yet

This file exists so that "not built" and "not thought about" stay different things.
Everything here has been considered and left out on purpose, with the reason
recorded. Where a decision is unlikely to be revisited, it links to the ADR that
settled it.

## Rejected, not deferred

**External autorouters** — Freerouting and the rest. Recorded in
[ADR 0008](decisions/0008-mech-placement.md#rejected) so it is not relitigated. A
foreign router breaks three properties the whole toolchain rests on: determinism
(its output depends on a time budget and an internal seed), source mapping (its
copper arrives as geometry with no relationship to the source that asked for it,
so a DRC violation on one of its tracks cannot be reported against a line in a
YAML file), and preserve semantics (M6 tells our copper from a human's by UUID,
and a third tool's is neither).

Where the router is not good enough, the answer is another *pattern generator* —
the M9e fanout generator is the worked example — or an honest hand-over.

## Mechanical (after M9)

**MCAD import.** `aipcb import-mech`, reading DXF or STEP reference points and
writing them into `board:` and `placement:` blocks. The YAML is deliberately
shaped so this becomes a pure generator: a `fixed:` block whose `reason:` points at
the mechanical file is exactly what an importer would emit. Nothing is built.

**3D clearance and height checking** against an enclosure model. Needs a solid
modeller and a height field per footprint; neither exists here.

**Back-side placement mirroring.** `side: back` validates, warns, and places on the
front. Mirroring a footprint means swapping every `F.`/`B.` layer pair inside it —
copper, mask, paste, silkscreen, courtyard, fabrication — and approximating it is
worse than refusing it.

**Panelization** — mouse bites, V-cut scoring, break-off rails, fiducials on the
frame. This is fab-level: a panel is a different object from a board, and modelling
it as an outline with holes in it would be a lie that survives right up until
somebody orders one.

## Routing

**Further pattern generators.** M9e establishes the architecture — a deterministic
generator that runs before routing, whose output is fixed obstacles plus terminals,
and which the rubber-band router then never has to know about. The obvious next
tenants:

* ~~differential-pair via transitions, with the return via that keeps the reference
  continuous across the layer change~~ — **built in M11c**, and the third tenant
  after fanout and stitching. `route/transition.py`;
* crystal and oscillator routing, where the guard ring and the short symmetric
  load-capacitor loops are a fixed shape rather than a search;
* antenna feeds, where the geometry *is* the specification.

**Pour-aware routing.** M10 pours copper and reports what the fill produced; it
does not let the pour influence where the router puts a track. The two things that
would need is return-path preservation (a track on the layer above a plane should
not cross a slot in it) and fragmentation-avoiding costs (a track that would cut a
plane in two should pay for it). Both are research: the router's cost model is a
per-layer congestion field, and a plane's connectivity is a global property of the
finished copper, so the feedback loop is not a term you can simply add. The
plane-integrity report is M10's answer — it tells the agent what the routing did to
the plane, and the agent can move a track or give it a layer. Recorded in
[ADR 0009](decisions/0009-pours.md).

**Current-capacity analysis of pours as conductors.** Net classes carry
`max_current_a`, and after M10 a plane is a real conductor with a measurable
cross-section — but nothing checks one against the other, and doing it properly
means current *distribution* over a shape rather than a width, which is a field
solve rather than a rule.

**Reimplementing zone fill** — rejected, not deferred. KiCad's fill is what DRC
checks against, so a second implementation would be checked against the first,
would differ, and the difference would be our bug on every board. Recorded in
[ADR 0009](decisions/0009-pours.md); the fill runs through KiCad's own engine in a
`pcbnew` subprocess, with a version lock so it is always the same engine that later
checks it.

**Making the *filled* board byte-stable.** Build output is byte-identical and
always will be; the filled copy is not, because KiCad's writer adds `Datasheet` and
`Description` properties with random UUIDs to footprints that lack them
([ADR 0009](decisions/0009-pours.md), Finding 5). Emitting those two properties
ourselves would close it, at the cost of changing the bytes of every board this
tool has ever written. Nothing depends on it today: the copper is stable, and the
copper is what the fabricator gets.

**Same-net sliver trimming.** Where two tracks of one net diverge at a shallow
angle they leave a wedge a few microns wide, which KiCad reports as a copper sliver
and a fabricator would rather not etch. `_join_existing_copper` trims the common
run but not the divergence. `examples/diff-pair` has one, and
`tests/test_check_loop.py` records it as a named known issue rather than silencing
the rule.

**M11 did not resolve it, and it is worth saying which of the two claims about it
is true.** The sliver is on the *unfilled* board -- `kicad-cli pcb drc` on
`aipcb build`'s output reports exactly one `copper_sliver` warning on
`examples/diff-pair`, and has since M8. On the *filled* board it is gone, because
the ground pour that M10 added fills the wedge with copper of the same net; the
`aipcb check` that runs DRC against the filled board has reported zero violations
on that example since M10 landed. The track geometry has not changed. M11d rule 1
does not touch it either way: the sliver is between two **GND** tracks, `ground`
is not a controlled-impedance class, and the standoff corridor applies to nothing
but pairs whose class names an `impedance_diff_ohm`.

**Length matching beyond a pair.** Skew is measured and reported within a
differential pair; matching a whole bus to a target length, with meanders, is not.

**Full environment-controlled tightening.** M11d bounded it to three rules on
purpose — a standoff corridor, a wall-hugging flag with one re-tighten, and a hard
coupling budget. Tightening that actually held a pair's *environment* constant
would mean optimising something other than length, which is the stretcher's whole
premise; recorded in [ADR 0010](decisions/0010-highspeed.md).

**Placing AC-coupling capacitors.** `role: ac_coupling` validates that the two
capacitors are level across the pair's direction of travel and measures how far
out they are; it does not move them. The placer is M9's, and a routing-side
generator that moved parts would be a larger change than M11 sanctioned.

**Back-drilling, and blind or buried vias chosen to shorten a stub.** M11e
measures the stub a through via leaves below the layers the signal uses and
reports it against a threshold. Removing it is a fabrication process nobody should
be committed to by a default.

**A route that crosses a differential lane.** `examples/pcie-sata` leaves two
contacts unconnected — PERST# and the A1-to-B17 presence-detect strap — because
both have to cross all three lane pairs and a four-layer sig/gnd/pwr/sig stackup
has no signal layer left to cross on. A real card uses an inner signal layer or a
wire link. Worth recording because it is the first board in this repository where
the honest answer to "route this net" is "not on this stackup".

**`_repair` can produce a route that crosses another net.** Found on that same
net before it was dropped: the second-pass repair (`route/plan.py::_repair`)
routed PRSNT across two already-placed `REFCLK` tracks, and KiCad reported two
`tracks_crossing` errors. The repair builds its private field from the base
environment *plus* everything placed so far, so the copper it crossed was in the
obstacle set; something between that field and the realized geometry is not
honouring it. Not chased to the bottom, because it is router work rather than
M11's, and recorded here rather than left as a surprise.

## Generated files

**Pads that share a number share a UUID.** A USB Micro-B receptacle has twelve pads
numbered 6 and an exposed pad is often split into several; the board writer derives a
pad's UUID from its *number*, so those come out identical. KiCad opens and checks such
a board — every bundled example has some — but duplicate identifiers are not something
a file format should contain, and a DRC violation on one of them cannot say which. The
fix is to key the UUID on the pad instance, as the router already keys its obstacles;
it changes the UUID of every affected pad, so it wants a milestone of its own rather
than a quiet rewrite of every golden file.

M11 measured how far it reaches, because a card-edge connector looked like the
place it would bite hardest. It depends entirely on the footprint:
`Connector_PCBEdge:BUS_PCIexpress_x1` numbers all 36 contacts distinctly and is
untouched by it, while the JST connector on the same board has two shell tabs both
numbered `MP` that share one UUID -- and `Connector_PCBEdge:BUS_PCI` has 240 pads
over 120 numbers, which is squarely in the defect's path. Both cases are asserted
as tests in `tests/test_highspeed.py`.

## High speed

**Electromagnetic simulation** landed in M12 as `aipcb simulate`, so what is left
here is what it deliberately does not do.

**Crosstalk between pairs, eye diagrams, IBIS driver models.** A slice carries its
neighbours' copper, so their loading is in the answer, but nothing excites them and
nothing reports coupling between two pairs. An eye needs a driver model and a
channel; M12 produces the channel. All three are the obvious next stage.

**Automatic fixes from simulation results.** Deliberately absent: the JSON exists so
that an agent loop can read a finding and change the source, which is where a fix
belongs. `aipcb simulate` will never edit a design.

**Thermal and power-integrity simulation.** Not attempted. The same toolchain can do
plane resonance and PDN impedance; nothing in aipcb asks it to.

**Simulation inside `check`.** It stays a separate command. A pair costs a minute or
two against seconds for the whole of `check`, and its output is engineering judgement
rather than a gate.

**The `.kicad_pcb` stackup disagrees with the source stackup.** Found by M12 and
*not* fixed, because fixing it changes every existing board.
`compile/board.py::_stackup` splits the board thickness evenly between the copper
layers and applies the first declared core's material to all of them, while
`Stackup.dielectric_between` — which is what impedance is derived from — honours the
`layers:` block. On `examples/pcie-sata` the prepreg under F.Cu is 0.2104 mm in the
source and 0.48 mm in the board file. Simulation uses the source; KiCad is told
something else. See [ADR 0011](decisions/0011-si-simulation.md), Decision 2.

**Per-dielectric material, and `epsilon_r` that is actually used.** The same
function takes the *first* core or prepreg entry's `material` and `epsilon_r` and
writes them into every dielectric, and types all of them `core` even on a four-layer
stack that alternates core and prepreg. `loss_tangent` became reachable from source
in M12; these two did not, for the same byte-stability reason.

**Impedance formulas that know about coplanar ground.** Both closed forms aipcb
carries are bare microstrip. Every bundled example pours ground up to its pairs at
the class clearance, and M12 measured what that does: the simulated impedance lands
well below what the formula derived the width for. The width a design is built with
is therefore systematically narrow. Fixing it means a coplanar-waveguide-with-ground
model and regenerating every controlled-impedance example.

**A digest-pinned Containerfile.** Antmicro's Dockerfile pins its base image by tag
and installs from moving apt archives, so `aipcb simulate` is pinned by *image*, not
reproducibly rebuildable. Carrying our own Containerfile is the only fix and means
maintaining a fork of their build.

**A second impedance formula.** `estimate_gap`, the M8 path that guesses a gap
from `impedance_ohm`, still uses Hammerstad while the controlled-impedance path
uses IPC-2141. On the M11 reference stackup the two disagree by 8%. They are kept
apart deliberately -- changing the old one would move the geometry of boards built
before M11 -- but a project with one impedance model would be better than one with
two, and unifying them means regenerating goldens.

**Impedance on an inner layer.** The derivation assumes surface microstrip, which
is what an outer layer is. A pair on an inner layer between two planes is
stripline, a different formula, and nothing stops a design asking for one today
except that the number it gets back will be wrong. There is no check for it.

**Copper roughness, solder mask and etch taper.** None of them is modelled. On a
0.24 mm trace over 0.21 mm of prepreg they are worth a few percent each, which is
the same order as the formula's own error -- which is why M11e reports geometry
rather than an impedance it does not have.

## Pours

**A ground pour for the two examples that have no ground.** `examples/congestion`
and `examples/overconstrained` declare four signal nets and nothing else; they are
routing-topology fixtures rather than boards, and pouring one of their signal nets
would be a fiction. They are the two of ten examples without a `pours:` block, and
they are also the live witness that a design without one still builds with no zone
in its output at all.

**Keepout zones.** `layout.placement.keepouts` keeps the *router* out of an area,
and the pour respects the outline and every cutout, but a KiCad keepout zone — the
kind that also excludes copper pour, drawn as a zone with a `keepout` block — is not
emitted. A pour region is the positive form of the same idea and covers the cases
the examples needed.

## Validation

**Exact placement infeasibility.** The M9c relative-intent check reasons with
intervals and reports only when the bound it computes already exceeds what the
constraint asks for. A complaint therefore means the constraint really cannot hold;
silence means nothing either way. Deciding satisfiability exactly is a constraint
problem in its own right and would need a solver.

**Thermal and current-density rules.** `via_count` in the fanout generator models
"one via carries about as much current as a track as wide as its barrel", which is
a rule of thumb standing in for a thermal simulation. Net classes carry
`max_current_a`; nothing checks copper against it.

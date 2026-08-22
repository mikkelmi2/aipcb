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

**~~`_repair` can produce a route that crosses another net.~~** **Fixed in M13a**,
and the cause was not in `_repair`. Finished copper is a *list* of obstacles and
the free-space calculation wants a *dict keyed by name*, so two pieces that shared
a name became one and the loser vanished from every triangulation built afterwards.
A differential pair split across two layers by a via transition produces exactly
that: one `RoutedConnection` per layer, both naming their coupled leg after the
same two pair terminals. On `examples/pcie-sata` four pieces of copper were being
dropped that way, silently, since M11c -- the B.Cu halves of `REFCLKP/N` and
`PCIE_RXP/N` -- and the repair pass that routed `PRSNT` across them was obeying an
obstacle set that no longer had them in it. `route/geometry.py::with_copper` now
suffixes rather than overwrites, `_accept` names copper by layer as well, and
`route/invariant.py` asks the finished board whether any two nets overlap. See
[`m13.md`](reports/m13.md).

## Verification

**Copper outside the board outline is not checked by anything.** Measured on KiCad
9.0.8 in M13.5: two tracks of different nets laid across each other *outside*
`Edge.Cuts` produce no `tracks_crossing`, no `clearance` and no
`copper_edge_clearance` -- only `track_dangling`, which is about their ends rather
than where they are. Nothing in aipcb puts copper there, and the cross-net invariant
M13a added would catch an overlap wherever it was, so this is a standing gap rather
than a live one. It is recorded because "DRC is clean" means less than it looks like
on a board with copper off the edge, and a test now pins the behaviour so a change
would be noticed.

**KiCad's DRC severity defaults are a moving target aipcb now pins.**
`compile/project.py::DRC_SEVERITIES` names a severity for the four rules KiCad 9.0.8
holds at `ignore`, because `--severity-all` does not include `ignore` and a rule held
there is dropped inside KiCad with nothing in the report saying a category went
missing. The list is version-specific by construction. ADR 0009's rule applies: it
wants re-measuring at each KiCad major, and `tests/test_check_loop.py` carries the
60-rule catalogue with the version attached so the diff is visible when it moves.

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

**M13d re-verified how far it reached during the M10-M12 chain, which was the open
question.** Every generated element on all eleven examples was walked and its UUID
attributed to its owner. 34 duplicated UUIDs across six examples, and **every one
of them is a pad**: the JST shell tabs on `pcie-sata` (`J2.MP` through `J5.MP`, two
tabs each), the QFN's unnumbered mechanical pads against its numbered ones, and the
same shapes on `enclosure`, `ldo-supply`, `mcu-4layer`, `qfn-fanout` and
`usb-port`. **No stitching via, no zone, and no edge-connector finger is affected**
-- the M11 reading holds, now measured across the whole corpus rather than argued
from two footprints.

**The same class of defect turned up in a place nobody had looked: track
segments.** Four `segment` items on `examples/pcie-sata` share a UUID in pairs, one
on F.Cu and one on B.Cu. `route/emit.py::track_uuid` keys a track on
`(net, leg.start, leg.end, index)`, and a differential pair split across two layers
by a via transition names its coupled leg after the same two pair terminals on both
-- exactly the collision M13a found in the *obstacle* set and fixed there. It is
not fixed here, because the obstacle name is internal and the track UUID is in the
file: adding the layer to it changes the UUID of every track this tool has ever
written. It belongs in the same milestone as the pad scheme, for the same reason
and with the same fix (key on the instance, not on a name that repeats).

## High speed

**Electromagnetic simulation** landed in M12 as `aipcb simulate`, so what is left
here is what it deliberately does not do.

**One simulated link out of eleven is not physical, and it is still `PCIE_TX`.**
M13.6 closed the other ten by giving every slice a connected return path, and left
the transmit link at `|Sdd21| = 1.220` with the coupling-capacitor bridges as the
one remaining suspect and an exact one-out-of-eleven correlation behind it.

**M13.7 tested the bridges and the answer is no.** The bridge's geometry was
measured off the slice artifact rather than argued about: same layer as the pads it
joins, same width as the trace, spanning exactly the 0.96 mm between the two cap
pads at the 1.4 mm pitch the board's own fanout puts them at. One defect was real
and it was not geometric -- **the bridge carried no net**, and it was the only
unnetted copper in any slice on the corpus, so gerber2ems's grid generator (which
adds mesh lines only for the nets in `netinfo.json`) never resolved the one piece of
copper sitting on the discontinuity. Fixed: the bridge is emitted in two halves,
each carrying the net of the pad it starts from. The mesh went from 0.78 M to
0.94 M cells and the coarsest cell across the bridge from 79.9 to 33.5 um.

**The number did not move.** `|Sdd21|` 1.220 -> 1.22, Zdiff 79.29 -> 79.59 ohm,
energy -64.6 -> -66.4 dB. So the mesh hypothesis is refuted, the fix is kept because
unnetted copper is a defect either way, and what `PCIE_TX` actually is remains open.

The new signature, which is more specific than the old one and is where the next
hypothesis should start: **the excess gain is already there single-ended.** At the
6.25 GHz peak, `|S(4,2)| = 1.112` and `|S(2,1)| = 1.003` -- one half of the pair
gains eleven per cent on its own, before any mixed-mode arithmetic, and the two
halves are not alike. The excess rises monotonically from 0.96 at 0.5 GHz to 1.22 at
6.25 GHz and falls above it. That points at the ports rather than at the interior:
this link launches at 0.5 mm pitch at the controller end and **1.0 mm at the card
edge**, against a coupled run at 0.3346 mm, so both ports sit on geometry whose
differential impedance is far from the 85 ohm the ports are terminated at, and they
sit on *different* such geometries. Testable the same way the last two hypotheses
were, and not in the milestone that had one hypothesis and finished it.

**`REFCLKP/N` converges, and what was trapping it was the slice's return path.**
Closed in M13.6, and the diagnosis M13.5 stopped at was two thirds of the answer.
`In2.Cu` floating was one half: it carries `P3V3`, the slice cuts away the supply,
the pads and the decoupling that make a supply plane a reference, and it reaches
the solver as a plate at no defined potential. The other half is that a slice was
losing the ground structure around it -- **22 of the 41 stitching vias inside the
eleven slice windows never reached a slice**, because `_assemble` keeps a via only
when its centre is inside the rectangle inset by 0.5 mm and a via in that band was
dropped silently. `REFCLK`'s slice contained *none* of M10's stitching; the five
`GND` vias M13.5 counted are 0.4 mm transition vias.

Measured one change at a time at 32 000 steps: the control plateaus at -7 to
-10 dB, a boundary fence alone at -9.5 to -10.8, tying `In2.Cu` alone at -13.6 to
-15.1 (which reproduces M13.5's i5 independently), and **both together decay
-9.8, -18.8, -29.3, -39.9 dB and keep falling**. Neither half works alone, which is
what makes them one idea: a slice's return path has to be a single conductor. At
full step count the link now reaches **-63.7 dB**, stops on energy decay rather than
on the step limit, comes back `usable`, and does it in **325 s against the 474 s**
the non-converging run cost. Its impedance did not move -- 49.5 ohm against 85 --
because that is the layer-spanning estimator below, and not the physics.

**The slicer cannot measure one side of a via transition.** M13.5 established that
the two links which miss the +/-10 % band -- `PCIE_RXP/N` and `REFCLKP/N` -- are
exactly the two whose ports sit on different layers, and that
`si/results.py::analyse` estimates impedance as a median input impedance, which is
the characteristic impedance of a *uniform* line. A link that changes layer is a
cascade of two sections and a barrel, referenced to two different planes, and the
median is not either section's Z0. The output now says so
(`Slice.spans_layers`, `Metrics.spans_layers`, and a note on the finding), but
saying so is not measuring it. Two ways forward, cheapest first: **widen the
sweep** -- at 8 GHz a TDR resolves about 4.5 mm and the F.Cu section is 2.6 mm, so
the existing S-parameters cannot separate them, while 20 GHz would -- and then
**port each side separately**, which is a slicer change.

M13.6 sharpened this rather than moving it. `REFCLK` now converges to -63.7 dB and
still reads -41.7 % against its target, which removes the last alternative
explanation: the number is not a truncated run and not a floating reference, it is
an estimator applied to a structure it does not describe.

**`--parallel N` is built, measured, and the measurement says do not use it here.**
ADR 0011 Decision 4's premise -- "openEMS already uses every core" -- was re-measured
in M13.6 and is false: the engine benchmarks itself at startup and settles on four to
six threads of this machine's sixteen whatever it is handed. That was the `CLAUDE.md`
pattern arriving for a third time, so M13.7 built the flag and measured the win
rather than assuming it.

**There is no win on this machine.** Three links, same slices: 693 s one at a time,
**1 023 s all three at once** -- 48 % slower, with aggregate throughput (316 MC/s)
*below what one solver gets alone* (489-619). Mid-run the three drew about 750 % of a
CPU against one solver's 600 %, so nothing was saturated except memory bandwidth --
which is also why the auto-tuner declines the spare cores, and a second process does
not create any. Decision 4's conclusion therefore stands on a reason it never gave.
Numbers in [ADR 0011](decisions/0011-si-simulation.md) Decision 4a.

What is left of it:

* **A host with more memory channels may go the other way**, and that is why the
  flag ships rather than being reverted. The measurement is a property of one memory
  system, and the tool now reports the throughput and thread count of every run so
  another machine's answer can be compared against this one's.
* **`--numThreads` is still unmeasured, and now also unreachable.** gerber2ems calls
  `openEMS.Run()` without it and the image is pinned, so the only lever aipcb has is
  how many CPUs the container is given. Whether the auto-tuner's four-to-six is
  optimal needs a patched image to answer -- and the concurrency result above is
  circumstantial evidence that it is close to right.
* **This host cannot pin a container to a cpuset**, so the measurement above is of
  *unpinned* concurrency and that confound is real. Rootless podman on cgroup v2 is
  delegated `cpu memory pids` and not `cpuset`; `--cpuset-cpus` is accepted by the
  CLI and refused by crun. `aipcb` probes for it and records which of the two it
  got. Granting it is a `Delegate=` drop-in on `user@.service` -- a root change to
  somebody's machine, and not something a PCB tool should require.

**No GPU route exists in this solver.** Also measured rather than assumed: the
image's `openEMS --engine` offers `basic`, `sse`, `sse-compressed` and
`multithreaded` and nothing else, `strings` on the binary finds no CUDA, OpenCL,
NVIDIA or HIP symbol, and `ldd` links no GPU library. FDTD suits a GPU very well
and commercial solvers get 10-50x from one; getting it here means a different
solver, not a flag.

**A slice still cannot say what it will cost before it runs.** Half of this is
closed: M13.6 made the per-pair budget a design parameter, `simulation.timeout_s`,
raised the default from 1800 s to 7200 and wrote the three measurements it is sized
from beside the constant. That stops the failure M13 hit -- a batch that reports
`failed 1800.0 s` rather than a number, eight times, because a timeout writes no
`result.json` and the next run re-slices from zero. What is still missing is the
*estimate*: the cell count and the step limit are both known before the container
starts, and the machine's own throughput is measurable, so a batch could say "this
is five hours" instead of finding out.

**REFCLK is in the wrong net class.** PCIe CEM r3.0 section 2.1.1 puts the
reference clock at nominal **100 ohm** differential; `examples/pcie-sata` carries it
in `pcie_rx` at 85 ohm, because it shares that class's layer and reference plane.
Giving it its own class is a geometry change, so M13.5 recorded it rather than made
it. Until then every REFCLK impedance number in every report is being compared to a
target the standard does not ask for.

**Four pairs are over the PCIe intra-pair skew requirement, and it is not a
budget error.** M13.5 checked the budget against PCI Express CEM r3.0 section 4.7.7
-- under 0.127 mm on an add-in card -- and the class was already at 0.125 mm, so
there is no specification fix available. `PCIE_TXP/N` (0.191 mm), `PCIE_TXP/N_C`
(0.247 mm), `PCIE_RXP/N` (0.219 mm) and `REFCLKP/N` (0.359 mm) need geometric work,
and the finding's own hint names it: move the end components so the two halves break
out symmetrically. Note that section 4.7.7's rationale is common-mode conversion and
therefore EMI, not timing -- so the "1-2 ps against a 125 ps unit interval"
argument M11, M12 and M13 all reached for does not answer it.

**Crosstalk between pairs, eye diagrams, IBIS driver models.** A slice carries its
neighbours' copper, so their loading is in the answer, but nothing excites them and
nothing reports coupling between two pairs. An eye needs a driver model and a
channel; M12 produces the channel. All three are the obvious next stage.

**Promoting a skew finding to an error.** M13c built the frequency-domain verdict
that M12's scalar could not be: fit the mode-conversion curve against the
`|sin(pi f dt)|` family, extract the implied delay, and compare *that* to the
class's budget. It reports as a warning and stays one. Promotion is gated on the
fit's false-positive behaviour being measured across a full board, and
`docs/reports/m13.md` records how much of that measurement exists.
[ADR 0012](decisions/0012-coplanar-impedance.md), Decision 5.

**An insertion-loss verdict cannot fail on excess gain.** `si/results.py` reports
insertion loss as `20*log10|Sdd21|` and compares it against a negative threshold, so
an extraction with `|Sdd21| > 1` reports *positive* dB and passes -- M13.6 measured
`SATA0_RXP/N` at +1.06 dB at 3 GHz with a `pass`. The `usable` gate catches the
gross cases at 1.15, but between unity and that gate the loss verdict is not
measuring anything. Surfaced by M13.6 and not caused by it.

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

**~~Impedance formulas that know about coplanar ground.~~** **Built in M13b**;
[ADR 0012](decisions/0012-coplanar-impedance.md). The derivation now reads the
`pours:` block, solves a coplanar model when ground will be poured beside the
class's layer, and publishes which model it used. One correction to what this
entry used to say: the widths were systematically **wide**, not narrow --
impedance falls as a trace widens, so a board reading below its target was built
too wide. `examples/pcie-sata` went from 0.2888 mm to 0.1846 mm on its 85 ohm
classes and from 0.2390 mm to 0.1379 mm on its 100 ohm one.

**A coplanar model for a pair on an inner layer.** M13b's model is a *surface*
coplanar waveguide with ground: a trace with ground beside it and one plane under
it. A pair between two planes with ground beside it is coplanar stripline, a
different formula again, and the existing "impedance on an inner layer" entry below
now has two gaps in it rather than one.

**Deriving the pour clearance from the impedance rather than the other way round.**
M13b made the pour gap an input to the width, and validation warns when it is tight
enough that an etch tolerance on it moves the impedance. Nothing solves the other
direction -- "how far should the pour stand off so this class is not sensitive to
it" -- which is a design question a tool could answer and does not.

**A digest-pinned Containerfile.** Antmicro's Dockerfile pins its base image by tag
and installs from moving apt archives, so `aipcb simulate` is pinned by *image*, not
reproducibly rebuildable. Carrying our own Containerfile is the only fix and means
maintaining a fork of their build.

**~~A killed client leaves its container running.~~** **Fixed in M13d.** A context
manager reaps on any exit, `SIGINT`/`SIGTERM` handlers and an `atexit` hook reap on
interruption, and a pre-flight check refuses to start a second solver against a
directory something is already writing -- which is the one of the three that
survives `SIGKILL`, since nothing can catch that. `si/runner.py`, and the
containers now carry an `aipcb.si.work` label so the pre-flight can find an orphan
whoever started it.

**Simulation cost now depends on the geometry, and can be large.** M13b found the
fixed 50 um mesh returning **12 Ohm for an 85 Ohm line** on the narrower traces the
coplanar model derives, with a clean exit and a converged energy decay
([ADR 0012](decisions/0012-coplanar-impedance.md), Decision 4). The mesh is now
derived from the trace and gap, which makes it correct and makes a fine-geometry
board several times more expensive to simulate. Nothing yet *warns* about the cost
before a batch starts, and a per-pair cell-count estimate would be cheap to print.

**The mode-conversion floor is not explained.** M13c fits an intra-pair delay out
of the frequency response and reports it beside M11e's geometric skew, which is
what makes the two verification layers comparable. What neither layer explains is
where the floor underneath comes from -- launch geometry, via barrels, the pour's
asymmetry around each trace, truncation noise -- and until it is understood the fit
is bounded from below by it. Characterising it means differencing two runs of one
pair that differ in exactly one thing, which is the experiment M12 named and did
not run.

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

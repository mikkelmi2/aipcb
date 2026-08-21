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

* differential-pair via transitions, with the return via that keeps the reference
  continuous across the layer change;
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

**Length matching beyond a pair.** Skew is measured and reported within a
differential pair; matching a whole bus to a target length, with meanders, is not.

## Generated files

**Pads that share a number share a UUID.** A USB Micro-B receptacle has twelve pads
numbered 6 and an exposed pad is often split into several; the board writer derives a
pad's UUID from its *number*, so those come out identical. KiCad opens and checks such
a board — every bundled example has some — but duplicate identifiers are not something
a file format should contain, and a DRC violation on one of them cannot say which. The
fix is to key the UUID on the pad instance, as the router already keys its obstacles;
it changes the UUID of every affected pad, so it wants a milestone of its own rather
than a quiet rewrite of every golden file.

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

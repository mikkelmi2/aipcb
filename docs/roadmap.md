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

**Zone pours.** A layer given over to a plane under `stackup.planes` is a
reservation today: the router stays off it and the nets that would have lived there
are routed as tracks. Pouring the copper — and with it thermal reliefs, spoke
counts, and stitching vias — is not built. It is what an exposed pad's thermal vias
should really connect to.

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

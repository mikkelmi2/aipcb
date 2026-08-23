# Study session — the gEDA toporouter postmortem

**2026-08-23. Research only. No code, schema, test or example changed; `src/` was not
touched.** The deliverable is
[`docs/notes/toporouter-postmortem.md`](../notes/toporouter-postmortem.md), plus
entries in [`roadmap.md`](../roadmap.md) for each candidate and each flagged exposure.

---

## What was studied, and under what constraint

gEDA PCB's *toporouter* — written by Anthony Blake in 2008–2009 as a Google Summer of
Code project mentored by DJ Delorie, from Tal Dayan's 1997 UCSC thesis on rubber-band
routing, on top of the GTS triangulation library. It is the only complete, shipped,
open-source implementation of topological routing that exists to read, and the closest
code-level ancestor aipcb's stretcher has. It is also a failure story: never finished,
broken by an unrelated refactor in 2011, never repaired, called "redundant (and
abandoned)" by its own project in 2015, and carried forward by neither of PCB's
successors.

**gEDA PCB is GPL-2.0; aipcb is Apache-2.0.** The note states this as a hard guardrail
and holds to it: ideas and lessons are not copyrightable and are fair game, code is
not. Findings are in the note's own words and diagrams; identifiers appear only as
addresses so a reader can find what is being described. Any future implementation of a
technique named there is written from the note and the academic sources.

## What was read, and what could not be

Read: the source in full (7,761 lines of `toporouter.c` plus its header); its 69-commit
history; Blake's own project page with its benchmark tables, recovered from the
Internet Archive; Launchpad bug #846789; four gEDA mailing-list threads.

Not obtained: **Dayan's thesis itself.** No open full text exists — ProQuest and Google
Books carry metadata only, and the SURF papers are paywalled. The note marks this
prominently and marks each claim about "what the thesis prescribes" as second-hand.
Also not found: any written rationale from pcb-rnd for declining to port the router.
Both gaps are recorded rather than filled with plausible narrative.

One correction to the public record, which the note carries because it is easy to get
wrong: the frequently-cited valgrind trace of a 2.3 GB blow-up in `heap_insert()` via
`BreakManyEdges()` is **PCB's maze autorouter, not the toporouter**. The two were
discussed interchangeably on the list and the record is polluted accordingly.

## What was learned

**The architecture is a convergent discovery.** The toporouter splits routing into a
*topological sketch* — an ordered sequence of cut crossings, no coordinates — and
*curvilinear wiring* computed only at the end. That is precisely aipcb's split between
symbolic negotiation and geometric stretching, arrived at independently and recorded in
[ADR 0006](../decisions/0006-routing-approach.md). Two projects, twenty years apart,
reading different literature, landed on the same decomposition.

**Where aipcb is ahead, and it is worth knowing why.** Its via column is a real object
on every layer its barrel pierces; the toporouter has *N* coincident 2-D obstacles, can
only reuse through-holes the board already had, and in the shipping rewrite had vias
switched off entirely — every published benchmark of it is single-layer. Its tightener
is the funnel algorithm, one linear sweep with a termination proof and a simple-output
guarantee; the toporouter's inserts the maximum-violation arc and recurses, with no
termination argument, and *detects* self-crossing output by deleting the offending arc.
And clearance: aipcb inflates obstacles before triangulating, so a legal path is what
falls out; the toporouter carries width and keepaway into the geometry pass and asks
"does this violate?" at every step. That one decision retires most of the failure list.

**Where aipcb has no counterpart at all.** Four things, and they are the note's §C:

* *Spreading.* aipcb's funnel returns the shortest path, so every track hugs the
  inflated hull of everything it passes even with millimetres unused alongside. M11d
  rule 1 gives room to controlled-impedance pairs and nothing else. The toporouter
  carries two distinct passes — even distribution, and force relaxation to a
  minimum-spacing equilibrium — and the simpler one operates on exactly the
  information `LayerField.used` already holds.
* *Post-convergence improvement.* aipcb's negotiation converges on legality and stops.
  Nothing asks whether a legal route is a good one. Blake's detour pass is the
  best-evidenced result in the whole record: **Meggy Jr 190.9 → 160.1 inches (−16%),
  Flare Genesis 52.8 → 48.0 (−9%)**, for 15–30% more runtime.
* *Cluster merging.* aipcb fixes a Euclidean MST over pad centres before routing;
  the toporouter merges a net's terminal groups as connections land, so later ones
  target everything already connected. Blake named EMST as a limitation of his own
  first version. It is also the *cause-side* fix for the `copper_sliver` that
  `examples/diff-pair` still carries and `_join_existing_copper` trims after the fact.
* *Special cuts.* The find of the study, below.

**One soundness gap, previously unknown.** aipcb states Maley's realizability criterion
and then charges only the triangulation's own diagonals. The second diagonal of every
adjacent triangle pair is a cut too, and can be the shorter one — a wire entering and
leaving a triangle through its other two edges never crosses the CDT diagonal at all,
but must still round the apex opposite it. Blake hit this, named them *special cuts*,
and rewrote his cluster code around them in July 2009. So `check_capacity` is
**optimistic**: it can pass a board the criterion rejects. The consequence is bounded —
the stretcher is constructive and `route/invariant.py` asks the finished board whether
two nets overlap, so what ships is a route that fails or detours absurdly, not a short
circuit — but the check claims more than it delivers, and its wording should be
corrected independently of whether the cuts are ever charged.

**The autopsy, root-caused.** The topological idea was never what broke. The fatal bug
was the CDT's floating-point predicates failing after the *host project* converted its
base units to nanometres: coordinates moved into a range the in-circle test had not
been tuned for, and edge-flipping recursed roughly ten thousand deep — on a board with
two resistors. Bisected by Andrew Poelstra in 2011 to a named commit. Confirmed,
priority Low, still open. Around it: assertion aborts that took down the host
application, segfaults on real boards, twelve minutes for a single connection, and over
a hundred hours of the author's time spent on the arcs-and-tangents exporter alone,
with a user-visible symptom still present a year later. **A topological router is a
geometry program wearing a graph program's clothes**; every fatal failure was a
predicate that stopped being true, and the routing search, the rip-up policy and the
cost model never failed once.

**And why nobody fixed it.** The commit log answers without ambiguity. The author's
last algorithmic commit is 2010-03-21. Everything after — thirty-odd commits, twenty-one
months, five people — is janitorial: warnings, `bool`, C++ compatibility, unit
conversion, dead-code removal. Peter Clifton did careful cleanup on the file in October
2011, *the month after the hang was bisected*, and did not attempt the hang. That is
the correct decision by someone who could read the code but could not hold its
algorithm. Three things compounded: the knowledge was never externalised (no design
document, no tests, a header comment admitting the file is undocumented); the rewrite
discarded the working version and was abandoned before regaining its via support; and
with no test suite, the only provably safe change was a cosmetic one — which is exactly
the history the log shows.

## The three-way verdict

The note's §B.3 grades aipcb against each identified failure mode as **guarded**
(invariant or test exists), **incidentally avoided** (architecture differs, nothing
watching), or **exposed**. Summary:

| | |
|---|---|
| **Guarded** | tightening termination; clearance and DRC in output (constructive *and* KiCad DRC on every example, every CI run); two nets' copper overlapping (`route/invariant.py`, which exists because M11 produced one and M13 found the cause); crashes taking down the host; correctness checked by tests rather than by looking at pictures |
| **Incidentally avoided** | CDT predicate robustness (ADR 0006 refused a hand-rolled CDT, explicitly, for this reason); memory exhaustion; fighting a host tool's data model (aipcb owns its model — the unit change that killed the toporouter has no analogue) |
| **Exposed** | runtime growth with board size, unmeasured; the capacity model's missing cuts; wires hugging obstacles with free space unused; legal-but-wasteful routes never improved; stability under small cost-parameter changes, untested |
| **Guarded by construction, unchecked in composition** | self-crossing geometry — one funnel output is simple, a multi-leg route across via hops is a concatenation of several, and nothing checks the join |

## What it changes

Six candidates and three flagged exposures, all now in
[`roadmap.md`](../roadmap.md) — the candidates under *Routing*, the exposures under
*Verification* — written so a milestone prompt can be drafted from either directly.

The ordering recommendation is the one thing worth repeating outside the note: **build
the benchmark harness first.** Three of the five quality candidates trade runtime for
quality, and without a baseline none of those trades can be judged. It also discharges
one of the three conditions autorouting must meet to leave beta. And runtime — not
correctness — is what actually made the toporouter unusable: ninety seconds and
thirty-five failed nets on a 269-net board, with no benchmark to see it coming.

The sustainability lesson does not fit in a candidate list, and is the reason the note
exists at all. The toporouter did not die of a hard algorithm. It died because one
person's undocumented, untested, seven-thousand-line understanding was load-bearing,
and when that stopped, a genuine research result became a file nobody else could safely
change — including, three years later, in the face of a routine upstream change that a
test suite would have caught in seconds. This project's ADRs, reports, invariants,
module boundaries and [re-measurement rule](../../CLAUDE.md) are the countermeasure to
exactly that. They do not change the fact that this is a one-maintainer router. The
toporouter is the evidence for what the residual risk looks like.

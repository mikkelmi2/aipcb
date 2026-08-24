# The gEDA toporouter: a postmortem

A study note. Nothing in `src/` changed in the session that produced it.

gEDA PCB's *toporouter* is the only complete, shipped, open-source implementation of
rubber-band topological routing that exists to read. It was written by Anthony Blake
in 2008–2009 — a Google Summer of Code project mentored by DJ Delorie — from Tal
Dayan's 1997 UC Santa Cruz PhD thesis, on top of the GTS constrained-Delaunay
library. It is the closest code-level ancestor aipcb's stretcher has. It also failed:
it was never finished, it was broken by an unrelated refactor in 2011 and never
repaired, it was called "redundant (and abandoned)" by its own project by 2015, and
neither of PCB's successors carried it forward.

Both halves are worth having. The techniques are free, and so is the autopsy.

---

## The licence wall

**gEDA PCB is GPL-2.0. aipcb is Apache-2.0. Study, never copy.**

Algorithms, ideas and lessons are not copyrightable and are fair game. Code,
comments, and line-by-line translations of code are not. Everything below is stated
in this note's own words and diagrams. Identifiers (`check_speccut`,
`oproute_rubberband_segment`, `src/toporouter.c`) appear only as *addresses* — so a
reader can find the thing being described — never as transcribed source.

Any future implementation of a technique named here is written **from this note and
from the academic sources**, not from the GPL file. If a candidate in [§C](#c-candidates)
is ever built, that provenance goes in its milestone prompt.

---

## Sources, and what could not be got

| Source | Status |
|---|---|
| Dayan, *Rubber-band based topological router*, PhD thesis, UCSC, 1997 | **Obtained in full at M18 — see [`routing-literature.md` §1.3](routing-literature.md#13-dayans-thesis-obtained--what-the-second-hand-markers-got-right-and-wrong).** The status below was true when this note was written and is left standing so the second attempt's result is legible; every claim in this note marked as second-hand should now be read against that section, which resolves them. In particular the thesis's shortest-path algorithm is *O*((T+S)² log(T+S)) with the shipping approximation at *O*((T+S) log(T+S)) and **not guaranteed shortest**; and §2.2.1 says SURF maintained each layer's mesh with an incremental constrained Delaunay triangulation. Original status: **Not obtained in full text.** No open PDF exists; ProQuest and Google Books carry metadata and an abstract only. Its content is cited here at second hand — through the SURF papers' abstracts, through Blake's own account of what he implemented, and through the bibliography the toporouter source itself carries. **Claims below about "what the thesis prescribes" are therefore weaker than claims about what the code does, and are marked where they appear.** |
| Dai, Dayan & Staepelaere, *Topological Routing in SURF: Generating a Rubber-Band Sketch*, DAC 1991; Staepelaere et al., *SURF*, IEEE D&T 1993; Dayan & Dai, *Layer Assignment for a Rubber Band Router*, UCSC-CRL-92-50 | Abstracts only; full texts paywalled. |
| `src/toporouter.c` (7,761 lines) and `src/toporouter.h` (484 lines), [russdill/pcb](https://github.com/russdill/pcb) | **Read in full for architecture and technique.** The primary source for §A. |
| Blake's own project page, `anthonix.resnet.scms.waikato.ac.nz/toporouter/`, last updated 2009-07-07 | **Recovered from the Internet Archive** (mirrored into the [bert/pcb wiki](https://github.com/bert/pcb/wiki/Autorouters:-gEDA-pcb-Toporouter)). This is the richest single source: benchmark tables, honest self-assessment, and the "why I restarted" statement. |
| The commit history of `src/toporouter.c` (69 commits, 2009-04-22 → 2011-12-24) | Read via the GitHub API. The maintenance story in §B.4 is read straight off it. |
| [Launchpad bug #846789](https://bugs.launchpad.net/pcb/+bug/846789), "toporouter broken" | Read. The proximate cause of death. |
| gEDA-user threads: [Apr-2010 "Toporouter Problems"](http://archives.seul.org/geda/user/Apr-2010/msg00244.html), [Sep-2011 "howto toporoute?"](http://archives.seul.org/geda/user/Sep-2011/msg00337.html), [Feb-2011 "What stops PCB autorouter/toporouter from working right?"](https://archives.seul.org/geda/user/Feb-2011/msg00488.html), [geda-dev GSoC announcement thread](https://geda-dev.seul.narkive.com/gWNGZKO5/gsoc-project-topological-autorouter) | Read. |
| narkive mirrors of "Toporouter update?" and "Toporouter VERY slow?" | **Unavailable** — both returned 5xx. The seul.org archive covered the same ground, so the gap is not believed to be material. |
| A written rationale from pcb-rnd for not porting the toporouter | **Not found.** pcb-rnd forked in 2013 and its Ringdove suite ships `route-rnd` plus a Specctra DSN bridge to Freerouting instead; no document was located stating *why* the toporouter was not carried over. Recorded as a gap rather than guessed at. |

**One attribution correction, because it is easy to get wrong.** The Feb-2011
gEDA-user thread that opens "what stops PCB autorouter/toporouter from working right"
carries a valgrind trace — an invalid 8-byte write in `heap_insert()`, ~2.3 GB
allocated, 1.76 M allocations against 268 k frees, SIGSEGV — reached through
`BreakManyEdges()`. **That call chain is PCB's maze autorouter (`autoroute.c`), not
the toporouter.** It is not evidence about the toporouter and is not used as such
here. The two routers were routinely discussed in one breath, and the record is
polluted accordingly.

---

## 0. What it was

```
 PCB board data                                                   PCB tracks + arcs
      |                                                                   ^
      v                                                                   |
 [import geometry] -> [CDT per layer group] -> [A* over CDT] -> [rubber-band export]
   pads, pins,          GTS constrained         "topological       "curvilinear
   vias, board          Delaunay; obstacle       sketch": an        wiring": tangent
   edge, existing       outlines become         ordered list of     lines and arcs
   lines                *constraint* edges      cut crossings       hugging obstacles
                                                      |
                                                      v
                                        [ROAR / RUBIX / detour rip-up loops]
```

Two representations, and the split is the whole design. A **topological sketch** is
the ordered sequence of triangulation edges a wire crosses — no coordinates, no
lengths, just which side of what. **Curvilinear wiring** is the geometry you get by
pulling that sketch taut. Routing happens entirely in the first; only at the very end
is the second computed. That is precisely aipcb's split between
[`negotiate.py`](../../src/aipcb/route/negotiate.py) ("negotiation is symbolic") and
[`stretch.py`](../../src/aipcb/route/stretch.py) ("topology in, DRC-clean geometry
out"), arrived at independently and recorded in
[ADR 0006](../decisions/0006-routing-approach.md).

Timeline, from the commit log and Blake's page:

| | |
|---|---|
| 2008 | GSoC project. First implementation. Routed simple boards; supported vias. |
| 2009-04-22 | **A second implementation, from scratch, is merged.** Blake's stated reason: rather than stabilise and document the first attempt, he judged his time better spent starting over — PCB already had "a highly optimized and efficient autorouter" and he doubted the first version could beat it. |
| 2009-06-10 → 2009-07-07 | The productive burst: one-pass curvilinear wiring, multiple traces per constraint edge, ROAR rip-up, a rewrite of the cluster and "speccut" code, detour optimisation. |
| 2009-07-07 | Last update to the project page. **Vias are off** — the rewrite's CDT never regained via support. |
| 2009-12-21 → 2010-03-21 | Four more commits from the author. Then nothing, ever again. |
| 2010-04 → 2011-12 | Thirty-odd commits from five other people. **Not one is algorithmic**: compiler warnings, C++ compatibility, `bool`, unit conversion, API churn, dead-code removal. |
| 2011-09-10 | Bug #846789: the toporouter hangs on a two-resistor board. |
| 2015 | Recorded in the project's own wiki as "generally considered to be redundant (and abandoned)". |

---

## A. Techniques worth learning

### A.1 Spreading — the one aipcb has no general mechanism for

The toporouter carries **two distinct passes** that push wires apart inside a
corridor, and the distinction between them is the useful part.

*Spacing* (`space_edge`) is a **force relaxation** run on the wires crossing a single
triangulation edge. Each crossing point feels a repulsion from its two neighbours in
the crossing order — and from the edge's own endpoints, so obstacles push too —
proportional to how much the required minimum spacing exceeds the actual gap. Points
move by a tenth of the residual per step; the loop runs to equilibrium, or **gives up
after a hundred iterations**, whichever is first. It is a legality repair: it only
pushes when something is *too close*.

*Spreading* (`spread_edge`) is different and much simpler. It ignores forces
entirely and **distributes the crossings evenly along the edge**: *n* wires on an
edge of length *L* land at *L*/(*n*+1) intervals, and a single wire lands at the
midpoint. It is a quality pass, and it runs whether or not anything was too close.

```
 obstacle A                                obstacle A
 ==========                                ==========
  ###                                        #
  ###   <- three tracks hugging               #      <- same three, spread
  ###      the inflated hull                   #        across the same gap
                                                #
                                               #
 ==========                                ==========
 obstacle B                                obstacle B
```

**Against aipcb.** aipcb's [funnel](../../src/aipcb/route/funnel.py) returns the
*shortest* path through the sleeve, which by definition hugs the inflated hulls. A
track therefore runs at exactly minimum clearance from every pad it passes, even
where three millimetres of free space sit unused on the other side. There is one
exception and it is narrow: **M11d rule 1**, the standoff corridor, tightens a
controlled-impedance pair's centre-line against `clearance × standoff_k` rather than
`clearance` ([ADR 0010](../decisions/0010-highspeed.md)). That widens the corridor
*for one class of net* by inflating obstacles more; it does not distribute anything,
and every other net still hugs.

The general mechanism is missing, and `spread_edge`'s form suggests it need not be
expensive: aipcb already knows, for every cut, exactly which routes cross it
(`RoutePath.crossings()` and `LayerField.used`), which is the same information
`spread_edge` operates on. **This is [candidate C1](#c1-a-spreading-pass).**

Blake's own framing is worth keeping: spreading is not cosmetic. Evenly distributed
wiring is what leaves room for the *next* net, which is why the pass runs during
routing and not only at the end.

### A.2 Vias — and the negative lesson

The toporouter's via model is startlingly thin, and the reason matters more than the
mechanism.

* Every pin and via is inserted as a vertex and an obstacle into **every layer
  group's** triangulation, independently. There is no object representing "a hole
  through the board" — there are *N* coincident 2-D obstacles that happen to share
  an (x, y).
* A route changes layer by walking onto a vertex that exists on both layers and
  continuing on the other layer's triangulation. The A* heuristic adds a flat
  `viacost` (default 100 mil expressed as distance) when the current and destination
  layers differ, and `path_score` charges the same per layer change.
* **The router cannot create a via.** It can only reuse a through-hole that the board
  already had. Blind and buried vias do not exist in the model at all.
* Fanout — pad to a fresh via — is a *separate, manual, non-topological* action
  (`escape`), which drops vias at fixed offsets from selected pads by pin pitch and
  draws the stub itself.
* And in the shipping rewrite, **vias were switched off entirely**. Blake's benchmark
  page says so in as many words: the new code "still requires work on the CDT to
  support my plans", so every published result is single-layer.

**Against aipcb.** The [via column](../../src/aipcb/route/field.py) is a strictly
better model and it is worth saying why in the toporouter's own terms: a via node is
one object, it is an obstacle on *every* layer its barrel pierces whether or not it
carries signal there, and it *joins* the layers' triangulations rather than being
independently rediscovered on each. aipcb's router places its own vias, prices them
through the cost model, bounds them per connection (`max_vias`), and M11c generates
matched pair transitions with return vias as a pattern.

The lesson is not in the mechanism, it is in the sequencing: **the toporouter's
rewrite shipped without the capability its predecessor had, and never got it back.**
It also anticipated aipcb's own answer to fan-out — a deterministic *pattern
generator* that runs before routing and hands the router fixed obstacles plus
terminals, which is exactly what M9e's `fanout.py` is. `escape` is that idea in
embryo, minus the determinism and minus the integration.

### A.3 The tightening

This is where the two projects differ most, and where the toporouter's history is
most instructive.

**What it does.** Given a path as a sequence of crossing points, `oproute_rubberband_segment`
works on one straight segment at a time between two anchors (a terminal, or a
previously-inserted arc):

1. For every triangulation vertex the path passes, compute how far that vertex
   *pushes* the segment — the violation depth, given the wire's own half-width and
   keepaway. Two cases are distinguished: the vertex's edge cuts across the segment,
   or it lies wholly to one side.
2. Take the **single largest violation**, and insert an **arc** of the required
   radius wrapping that vertex, with a winding direction.
3. **Recurse** on the two sub-segments either side of the new arc.
4. On unwind, look at the joint between the new arc and its neighbours, and delete a
   neighbouring arc if the tangent construction says it has become redundant.
5. Finally, walk the whole finished route and delete any arc whose entry and exit
   tangents cross each other — a **loop**. The loop check restarts from the beginning
   of the route each time it deletes something.

The output is genuine **curvilinear wiring**: straight tangent lines joined by arcs
that hug obstacle vertices at exactly the legal radius. It is the most faithful
rendering of "rubber band" anything in the open-source record.

**What the thesis prescribes** — *stated at second hand; the thesis was not
obtained.* The SURF line of work describes the rubber-band sketch as a
*representation* with well-defined geometric transformations, and Dayan's abstract
claims a proof of correctness for a shortest-path algorithm over it. The toporouter's
tightener carries no such proof, has no termination argument, and its own author
described the file as experimental and undocumented in its header comment. **This gap
— between an algorithm with a correctness proof in a thesis and a
maximum-violation-plus-recursion heuristic in the shipped code — is where the
toporouter's worst bugs lived.** §B.1 has the evidence.

**Against aipcb.** aipcb tightens with the [funnel algorithm](../../src/aipcb/route/funnel.py)
(Tompa 1981; Chazelle 1982; Lee & Preparata 1984; Leiserson & Maley 1985) over a
sleeve of triangles. The differences are not stylistic:

| | toporouter | aipcb |
|---|---|---|
| Input | a path through vertices | a sleeve of triangles (the homotopy class) |
| Method | insert the max-violation arc, recurse | one linear sweep, apex + two concave chains |
| Termination | no argument; delete-and-restart loops | provably linear in sleeve length |
| Result is simple (non-self-crossing) | **not guaranteed** — hence the loop checks | by construction: the shortest path in a simple polygon is simple |
| Clearance | applied *during* tightening, per obstacle, from wire width and keepaway | applied *before* triangulating, by inflating obstacles |
| Output | tangent lines + arcs | polyline, mitred |
| Determinism | claimed; results shifted under small parameter changes | tested (`test_stretching_is_deterministic`, `test_routing_is_byte_stable`) |

The row that matters most is **clearance**. The toporouter carries the wire's width
and keepaway all the way into the geometry pass, and asks at every step "does this
violate?". aipcb inflates the obstacles first, so the shortest path through what is
left is *legal by construction* — there is no violation to detect, and therefore no
violation-detection bug to have. That single decision retires most of §B.1.

### A.4 Ordering and rip-up

Four mechanisms, layered:

* **RUBIX** — a first pass in a greedy order. The order comes from a pairwise
  scoring: each unrouted net is routed alone against an empty board, then scored
  against every other net's solo route by how much they conflict, and the resulting
  matrix is reduced to a sequence.
* **ROAR** (rip-up-and-reroute) — route a net *allowing it to conflict*, then rip up
  everything it conflicted with and re-route those. Failures during recovery are
  counted, and past a threshold the whole attempt is **rolled back** to a checkpoint.
  It runs up to six passes, alternating a threshold of 2 and 5, and stops when the
  failure count stops falling.
* **Least-invalid search** — during a ROAR route the A* cost function is switched into
  a mode where crossing another net is not forbidden but *priced*, at the conflicting
  route's own score, scaled by the square of the number of conflicts so far.
* **Detour optimisation** — the most interesting one. After congestion has settled,
  score every *routed* net by how much its realised length exceeds its
  detour-free length. Sort descending. For each net whose excess exceeds a threshold,
  rip it up and re-route it, keeping the result only if it improved. Stop at the
  first net under the threshold.

**Against aipcb.** [Negotiated congestion](../../src/aipcb/route/negotiate.py)
(PathFinder, McMurchie & Ebeling 1995) is a better answer to the *first three*.
Priority plus difficulty orders the first pass; every net routes every iteration;
over-subscribed cuts get more expensive; a history term stops two nets swapping a
corridor forever; the loop has an iteration cap and a patience counter and reports
loudly when it does not converge. Where ROAR does explicit checkpoint/rollback with
hand-tuned thresholds, aipcb subtracts a demand from a cut and moves on. This is the
clearer design and it should stay.

**The fourth has no aipcb counterpart at all.** aipcb's negotiation converges on
*legality* — no cut over-subscribed — and then stops. Nothing afterwards asks whether
a route that is legal is also *good*. Blake measured what that pass is worth, and the
numbers are the best-evidenced result in the entire record:

| Board | Nets | Wiring length before | after detour opt | best |
|---|---|---|---|---|
| Flare Genesis | 123 | 52.79 in | 49.32 in | **47.99 in** (−9.1%) |
| Meggy Jr RGB | 158 | 190.87 in | 188.73 in | **160.08 in** (−16.1%) |
| Test | 11 | 8.97 in | **8.32 in** (−7.3%) | — |

For 15–30% more runtime. **This is [candidate C3](#c3-a-detour-pass--rejected-at-m19).**

### A.5 Clusters — routing to a net, not to a pad

A net is imported as a **cluster** of terminals, not a list of pads. A connection is
a *cluster-to-cluster* problem: the A* seeds its open list with every vertex of the
source cluster and terminates on *any* vertex of the destination cluster. And when a
connection completes, the two clusters are **merged**. The next connection of that net
therefore targets everything already connected, not a pad chosen in advance.

**Against aipcb.** [`spanning_routes`](../../src/aipcb/route/plan.py) fixes a
Euclidean minimum spanning tree over pad centres *before anything is routed*, and
every connection is then a fixed pad-to-pad problem. Blake named the same choice as a
limitation of his own first implementation in his GSoC wrap-up — that results could be
improved "through topologies other than Euclidean minimum spanning tree" — and the
cluster merge is what he built instead.

There is a second reason to care, and it is specific to a known aipcb defect. Because
a net's own copper cannot be an obstacle (or two connections of a net could never
meet), a later connection happily runs the length of an earlier one before branching,
and where the two finally diverge they leave a wedge KiCad reports as a
`copper_sliver`. aipcb handles this **after the fact**, by trimming
(`_join_existing_copper`); `examples/diff-pair` still carries one on the unfilled
board and `tests/test_check_loop.py` records it as a named known issue. Cluster
merging removes the cause instead of the symptom: a route that targets the whole
existing net ends *where it first reaches it*, and there is nothing to trim. **This
is [candidate C4](#c4-route-to-the-net-not-to-the-pad).**

### A.6 Special cuts — the technique aipcb most clearly lacks

This is the find of the study, and it is a soundness issue rather than a quality one.

Both routers rest on **Maley's realizability criterion**: a set of topologies can be
turned into legal geometry exactly when no *cut* across the free space is
over-subscribed. aipcb states this in [`field.py`](../../src/aipcb/route/field.py) and
implements it as: *every interior edge of the triangulation is a cut*, capacity is
the diagonal's length plus one clearance, and each route crossing it consumes its own
width plus its clearance.

**But the triangulation's edges are not all the cuts.** Blake hit this, named the
missing ones *special cuts*, and rewrote the code around them in July 2009. The
construction:

```
        opv                                opv
        / \                                /|\
       /   \                              / | \
    e1/     \e2                          /  |  \
     /   t   \                          /   |   \
    /         \                        /    |    \
  v1-----e-----v2      the cut that   v1    |    v2     <-- e, the CDT diagonal,
    \         /        actually binds   \   |   /           is NOT the binding
     \  opt  /         is opv--opv2      \  |  /            constraint here
      \     /                             \ | /
       \   /                               \|/
        opv2                               opv2
```

Two triangles `t` and `opt` share the diagonal `e`. A wire entering `t` through `e1`
and leaving through `e2` never crosses `e` at all — but it must physically round the
apex `opv2` of the *opposite* triangle, and the room available to do that is the
perpendicular distance from `opv2` to `e`. When the wiring that has to round `opv2`
exceeds that height, the real constraint is the **other diagonal of the quadrilateral**,
`opv–opv2` — a segment the constrained Delaunay triangulation did not choose and
therefore never charges. The toporouter detects the condition and **synthesises the
missing edge on the fly**, migrating the relevant crossings onto it.

**Against aipcb.** aipcb charges CDT diagonals and nothing else. The flip diagonal of
every adjacent triangle pair is a cut it never considers, and so is any other segment
between two obstacle vertices. `check_capacity` in
[`check.py`](../../src/aipcb/route/check.py) is therefore **optimistic**: it can pass
a board that Maley's criterion rejects.

How bad is that in practice? Less bad than it sounds, and the reason is aipcb's
architecture rather than any guard: the stretcher is *constructive*, it tightens each
route against inflated obstacles that include all previously finished copper, and
[`check_no_crossings`](../../src/aipcb/route/invariant.py) asks the finished board
whether any two nets overlap. So an under-counted cut does not ship a short circuit —
it shows up as a route that fails, or one that takes an absurd detour, in a place the
capacity report said was fine. **Exposed on completeness and diagnosis, guarded on
legality.** [Candidate C2](#c2-flip-diagonal-cuts), and [flagged exposure E1](#e1-the-capacity-model-under-counts-cuts).

### A.7 Other things it has that aipcb does not

* **Ordered, pairwise capacity accounting.** The toporouter computes a cut's
  occupancy by walking the crossings *in their order along the cut* and summing the
  pairwise minimum spacing of each adjacent pair, including the cut's own endpoints,
  with a special case for the wire that terminates there. aipcb sums `width + clearance`
  per route, order-independently. For a straight cut with uniform net classes the two
  agree exactly, so this is not a defect — but the toporouter's form generalises to
  mixed clearances between adjacent wires, which aipcb's does not.
* **Multiple wires on a constraint edge.** A "constraint" is an obstacle boundary. The
  toporouter allows wires to be ordered *along* one, which is what lets several tracks
  run down the side of a package. aipcb gets the same effect from inflation plus a
  free-space triangulation, so this is a difference in machinery, not capability.
* **A serpentine/trombone construction for length matching**, present in the geometry
  layer with a half-cycle count derived from the required delta. aipcb has
  [`meander.py`](../../src/aipcb/route/meander.py) for pairs; the roadmap's "length
  matching beyond a pair" is the unbuilt part.
* **Curvilinear (arc) output.** aipcb emits mitred polylines. §B says why not to
  envy this.
* **Cairo debug rendering of the triangulation, per pass, to PNG.** Compiled behind a
  flag. For a router whose failures are geometric, the ability to *look at* an
  intermediate state is not a luxury; it is how you debug at all. aipcb has
  `contested_cuts` and the JSON report, which is the same instinct in text.

---

## B. The autopsy

### B.1 What actually broke

Concretely, from the record:

| Class | Evidence |
|---|---|
| **Non-termination in the triangulation** | Bug #846789: the toporouter hangs, pinning a core, on a board with **two resistors**. Andrew Poelstra bisected it to commit 97b3260, "convert PCB's base units to nanometers", which changed coordinate types and the `LARGE_VALUE` constant. Stack traces showed nested `swap_if_in_circle()` calls "roughly 10,000 deep" from `gts/cdt.c:506`. The CDT's in-circle predicate stopped being reliable once coordinates moved into a range it had not been tuned for, and edge-flipping never converged. **Confirmed, priority Low, never fixed. Still open, with a 2019 comment offering a mouse-click workaround for the UI freeze.** |
| **Assertion aborts as error handling** | April 2010, PCB 1.99z: `import_route: assertion failed: (routedata->src->netlist == routedata->dest->netlist)`, abort trap. A netlist bookkeeping inconsistency takes down the entire application. `g_assert` is used pervasively in the router's hot paths, including inside the A*. |
| **Segfaults on real boards** | September 2011, a 388-pad layout: the run "took about an hour before it crashed with segfault". |
| **Runtime** | Same report: **twelve minutes for a single connection**, with the GUI wholly unresponsive. Ten connections took about the same. The user's conclusion — "autorouting of a single trace takes just about as long as routing the whole layout" — is a statement about the architecture: nothing is incremental. |
| **The geometry exporter** | Blake spent, by his own account, **well over 100 hours** on the topological-sketch → curvilinear-wiring exporter, "many fixes that didn't work in all cases or introduced new bugs". The June 2009 commit log is that fortnight in miniature: arc orientation in export checks (twice), arc removal not updating a vertex link, "traces arcing back around vertices". A user-facing symptom survived it: fanouts from pads to vias made a curvilinear-wiring error *more* likely unless the stubs were perfectly straight. |
| **Quality on real boards** | Blake, on his own project page: the algorithms "excelled when presented with many layers of free (channelless) wiring space" but "when applied to typical PCB problems, and especially when constrained to few layers and with dense constraints, the results were poor." |

And the benchmark table, which is the honest summary. All figures Blake's, on an Intel
E5300, **with vias disabled**:

| Board | Nets | Maze router | Toporouter |
|---|---|---|---|
| Puzzle (contrived) | — | fails | 0.08 s, solved |
| R8C/27 adapter (angled pads) | 44 | fails all | 0.89 s, 41 routed |
| Flare Genesis | 123 | <1 s, all routed, 60.7 in | 2.3 s, all routed, 52.8 in |
| Beer controller (1 layer) | 72 | ~1 s, 8 failed | 0.85 s, all routed |
| Laminator | 111 | ~2–3 s, 7 failed | 10.2 s, 2 failed |
| Meggy Jr RGB | 158 | ~6 s, 13 failed | 45 s, all routed |
| PPS splitter | 158 | ~9 s, 48 failed | 65.9 s, 31 failed |
| Photo diode receiver | 142 | ~4 s, 30 failed | 6.2 s, 28 failed |
| Altera FLEX 6024A | 269 | 27 s, 43 failed | **89.8 s, 35 failed** |

It is genuinely better on quality nearly everywhere, and 3–20× slower. The bottom row
is the one that killed it: on the largest board tested it spent a minute and a half to
beat the incumbent by eight nets out of 269, and left 35 for a human.

### B.2 Root cause

Three causes, and they are not equally weighted.

**Implementation robustness — the largest.** The fatal bug was not in the routing
algorithm; it was the CDT's floating-point predicates failing under a coordinate range
change made by someone else, in a library the router did not own. The 100-hour
exporter saga was arc-tangent geometry with no invariant to check itself against. The
crashes were `g_assert` and raw C memory management. **The topological idea was never
the thing that broke.**

**Architecture — real, and self-inflicted.** The tightener has no termination
argument and no simplicity guarantee, so it needs loop *detection* — and its response
to detecting a loop is to **delete the offending arc**, which is discarding output
rather than fixing it. There is no automated test of any kind: correctness was
checked by looking at rendered PNGs. Seven thousand seven hundred lines in one file,
whose own header says "it isn't documented or optimized". The rewrite dropped via
support and never regained it.

**Integration — real, and the least of the three.** The router re-imports PCB's data
structures on every run and rebuilds every triangulation from scratch, which is why
routing one connection costs the same as routing the board. It has no dialog, no
parameters a user can reach, and one undocumented action name. Blake's own advice to
users was to edit `hybrid_router()` in the C source and recompile.

**The unifying pattern.** A topological router is a *geometry* program wearing a
graph program's clothes. Its correctness rests entirely on predicates over floating
point — winding, in-circle, intersection, tangency — and every one of the fatal
failures was a predicate that stopped being true. The routing search, the rip-up
policy, the cost model: none of them ever failed.

### B.3 How aipcb fares, mode by mode

Honest three-way verdict. **Guarded** = an invariant or test exists.
**Incidentally avoided** = the architecture differs, but nothing is watching.
**Exposed** = same weakness, nothing guarding it.

| Failure mode | Verdict | Why |
|---|---|---|
| CDT non-termination / predicate failure under a coordinate-range change | **Incidentally avoided** | [ADR 0006](../decisions/0006-routing-approach.md) explicitly refused a hand-rolled CDT — "a classic source of subtle, data-dependent bugs" — and takes GEOS's via Shapely. Coordinates are millimetres snapped to `_QUANT = 1e-6`, which at board scale leaves eight orders of magnitude of double precision in hand. **But nothing tests scale robustness**: there is no case with a board far from the origin, an outline of unusual size, or near-degenerate obstacle geometry. This is the exact shape of the bug that killed the toporouter, and it is the shape [CLAUDE.md](../../CLAUDE.md) warns about — a premise about a tool that was true when written. |
| Tightening fails to terminate | **Guarded** | The funnel is one linear sweep, amortised O(1) per diagonal. There is no loop to not terminate. |
| Tightening produces self-crossing geometry | **Guarded by construction, unchecked in composition** | A single funnel output is simple because the shortest path in a simple polygon is simple. A *multi-leg* route (via hops) concatenates several funnel outputs, and nothing asks whether the concatenation is simple. See [E2](#e2-nothing-checks-that-a-net-does-not-cross-itself). |
| Output violates clearance / DRC | **Guarded, twice** | Clearance is satisfied by inflating obstacles before triangulating, so the shortest legal path *is* the shortest path. On top of that, `test_routed_board_has_no_drc_violations` runs KiCad's own DRC over every example on every CI run. |
| Two nets' copper overlaps | **Guarded, and learned the hard way** | [`invariant.py`](../../src/aipcb/route/invariant.py) exists because M11 produced exactly this and could not explain it; M13 found the cause was an obstacle dict silently overwriting same-named copper. Its docstring is the best single argument in this repository for why a constructive design still needs a blunt final check. The toporouter had no equivalent, which is why its exporter bugs lived for over a year. |
| A crash takes down the host application | **Guarded** | Failures are `StretchError` / handover records with a named reason; `aipcb route` reports and continues. There is no assertion in the router that aborts a process. |
| Memory exhaustion / leaks | **Incidentally avoided** | Python. Not a virtue, but not a risk either. |
| Fighting the host tool's data model | **Incidentally avoided** | aipcb owns its model and writes KiCad files itself ([ADR 0001](../decisions/0001-kicad-io.md), [0002](../decisions/0002-source-format.md)). The unit change that killed the toporouter was made by the host project to satisfy a constraint the router knew nothing about; aipcb has no such upstream. |
| Runtime growing badly with board size | **Exposed** | Nothing measures it. The [roadmap's graduation conditions](../roadmap.md#maturity-and-graduation) already name "runtime is benchmarked against board size" as a condition for autorouting to leave beta, so this is a *known* gap rather than a new finding — but the toporouter is direct evidence that it is the gap that kills, and it is worth raising its priority accordingly. The examples are all small; nobody knows the exponent. |
| The capacity check passes a board that cannot be built | **Exposed** | §A.6. Non-Delaunay cuts are never charged. Mitigated on *legality* by the constructive stretcher and `check_no_crossings`; not mitigated on completeness or diagnosis. |
| Wires hug obstacles with free space unused | **Exposed** | §A.1. No spreading pass. M11d rule 1 covers controlled-impedance pairs only. |
| Legal-but-wasteful routes never improved | **Exposed** | §A.4. Negotiation converges on legality and stops. |
| Correctness checked by looking at pictures | **Guarded** | 71 routing tests, golden files, byte-stability and determinism tests, DRC in CI. |
| Sensitivity to hand-tuned thresholds | **Partly exposed** | Blake reported that "subtle variations in the algorithms" — not randomness; he was explicit that nothing is probabilistic — produced three different complete routings of one board, and warned users off tuning for a specific board. aipcb's [`costs.py`](../../src/aipcb/route/costs.py) has the same character: iteration counts, `congestion_growth`, `PATIENCE`, `_ROOMY_MULTIPLE`. Determinism is tested; *stability under small parameter changes* is not, and no example is a fixture for it. |

### B.4 Why nobody fixed it

The commit log answers this without ambiguity.

**The author's last algorithmic commit is 2010-03-21.** Everything after it — more
than thirty commits over twenty-one months, from five different people — is
janitorial: compiler warnings, `bool`, C++ compatibility, `static inline`, dead-code
removal, an API rename, the unit conversion. Peter Clifton in particular did careful,
substantial cleanup work on the file in October 2011, *the month after the hang was
bisected*, and did not attempt the hang. That is not neglect. It is the correct
decision by someone who could read the code but could not hold its algorithm.

Three things compounded:

1. **The knowledge was never externalised.** No design document beyond a project page
   of screenshots; a header comment that says outright the code is experimental and
   undocumented; no tests. When the author left, the model of how it worked left too.
2. **The rewrite discarded the working version.** Blake chose "start from scratch"
   over "stabilise and document", explicitly, and the second version was abandoned
   before it regained the first's via support. The project ended up with neither a
   documented v1 nor a finished v2.
3. **Nobody could tell working from broken.** With no test suite, "is this change
   safe" was unanswerable, so the only safe change was a cosmetic one — which is
   exactly the change history the log shows.

**The lesson for a one-maintainer router**, stated plainly because it is the one this
project most needs to hear: the toporouter did not die of a hard algorithm. It died
because a single person's undocumented, untested, seven-thousand-line understanding
was the only thing holding it together, and the moment that person stopped, the code
became unmodifiable by anyone else — including, three years later, by a routine
change in the host project that a test suite would have caught in seconds.

aipcb's structural answer is already in place and is worth naming: ADRs that record
why, reports that record what was measured, invariants that fail loudly, a 24-module
package rather than one file, 71 routing tests, and the
[CLAUDE.md](../../CLAUDE.md) rule that a premise about an external tool must be
re-measured before it is built on. That is a genuinely different posture. It does not
change the underlying fact that this is a one-maintainer router, and the toporouter is
the evidence for what the residual risk looks like.

**One thing not to envy: the arcs.** Curvilinear output is beautiful and it consumed
more of the author's time than any other single thing in the project — over 100 hours
on the exporter alone, with the last user-visible symptom still present a year later.
aipcb emits mitred polylines. If arc output is ever proposed, this note is the
argument against doing it as part of the tightener; it belongs, if anywhere, in a
separate, independently testable post-pass over finished polylines.

---

## C. Candidates

Six, plus two flagged exposures. Each is scoped so a milestone prompt can be drafted
from it directly. **Source discipline: every one is to be implemented from this note
and the cited academic work, never from the GPL source.**

### C1. A spreading pass

* **Technique** — distribute the routes crossing a cut evenly along it, rather than
  leaving each hugging the inflated hull it was tightened against. Two variants exist
  in the record: even distribution, and force relaxation to a minimum-spacing
  equilibrium with an iteration cap. Even distribution is simpler, cheaper and has no
  convergence question; start there.
* **Source** — observed mechanism (`spread_edge`, `space_edge`); the underlying idea
  is the rubber-band sketch's freedom to slide crossings along a cut, from the SURF
  line of work.
* **Touches** — a new post-pass between `negotiate` and `stretch`, using
  `LayerField.used` and `RoutePath.crossings()` for the per-cut crossing sets, and
  feeding the stretcher an offset target on each portal. `funnel.py` gains a variant
  that tightens toward a *nominated point* on each portal rather than to the extreme.
* **Benefit** — every net gets what M11d rule 1 currently gives controlled-impedance
  pairs only: room. Less coupling, more space for later nets, easier hand editing,
  and it attacks the `copper_sliver` class from the geometry side.
* **Size** — medium. The pass is small; the funnel variant is the real work, and the
  golden files all move.
* **Note** — this generalises M11d rule 1 rather than replacing it. The standoff is a
  *hard* requirement for an impedance-controlled pair; spreading is best-effort.

### C2. Flip-diagonal cuts

* **Technique** — add, to the cut set, the second diagonal of every quadrilateral
  formed by two adjacent triangles, with capacity equal to that segment's length. A
  route is charged to it when its crossing sequence enters and leaves the pair through
  the two edges that the flip diagonal separates. Optionally also the triangle-height
  test: the perpendicular distance from a triangle's apex to its opposite edge is an
  upper bound on what can round that apex.
* **Source** — observed mechanism (the "special cut", `check_speccut`,
  `triangle_interior_capacity`); the principle is Maley's realizability criterion,
  which aipcb already cites and which quantifies over *all* cuts, not over one
  triangulation's edges.
* **Touches** — [`triangulate.py`](../../src/aipcb/route/triangulate.py) (derive the
  extra cuts alongside `diagonals`), [`field.py`](../../src/aipcb/route/field.py)
  (capacity, `used`, `history` arrays extend), `check.py::check_capacity`.
* **Benefit** — `check_capacity` becomes sound rather than optimistic. Congestion
  pressure appears where it really is, which should improve completion on
  `examples/congestion` and make its diagnostics truthful.
* **Size** — small to medium. The cut derivation is straightforward; the crossing
  attribution needs care, and every congestion-sensitive golden moves.
* **Do first** — measure. Count, on each bundled example, how many flip diagonals are
  *shorter* than the CDT diagonal they pair with. If the answer is near zero on real
  boards, this is a correctness tidy-up rather than a quality win, and should be
  scoped as such.

### C3. A detour pass — rejected at M19

* **Technique** — after negotiation converges, score every routed connection by
  realised length minus unobstructed length. Sort descending. For each above a
  threshold, rip it up, re-route it against the settled field, and keep the result only
  if it is shorter and still legal. Stop at the first connection under the threshold.
* **Source** — observed mechanism (`detour_router`, `roar_detour_route`), and Blake's
  own published before/after measurements (§A.4).
* **Touches** — [`negotiate.py`](../../src/aipcb/route/negotiate.py) (a post-convergence
  phase; rip-up already exists and is free), [`plan.py`](../../src/aipcb/route/plan.py)
  (report the improvement), [`costs.py`](../../src/aipcb/route/costs.py) (threshold,
  pass budget).
* **Benefit** — measured at 7–16% less copper on the comparable historical boards, for
  15–30% more runtime. aipcb has nothing in this space at all.
* **Size** — medium, and the smallest-risk of the three quality candidates: it is
  strictly a post-pass, it can only accept a strictly-better result, and it can be
  budget-capped and switched off.
* **Guard it** — the pass must be deterministic and must never accept a route that
  fails `check_no_crossings`. Bound it by iterations, not by wall clock, or
  `test_routing_is_byte_stable` will start failing on a loaded machine.
* **Measured at M17, in part.** This candidate's named first customer was the E2
  finding on `examples/pcie-sata` — the GND connection laying eight millimetres of
  copper and two vias twice over. It was fixed without a detour pass: M17's via
  pass asks a much narrower question (does this span fit on one layer, in one leg)
  and the answer was yes. So C3 has lost its concrete customer and keeps its
  general one; the numbers M17 measured while looking are in the
  [M17 report](../reports/m17.md) §1, and the useful one for anybody who does
  build C3 is that **53 of the 55 collapsible spans in the corpus were rejected
  because the on-layer route is longer or does not exist** — the vias this router
  spends are, with four exceptions, load bearing.
* **Re-priced at M18, downward.** The 7–16% above is Blake's, on two boards. Dayan's
  own measurement of the same mechanism — his ROAR optimiser, ten two-layer bins,
  427 branches — is **detour from 8.84% to 5.18%, about 3.4% of wire length**
  ([`routing-literature.md` §1.3](routing-literature.md#13-dayans-thesis-obtained--what-the-second-hand-markers-got-right-and-wrong)).
  That is a primary source measuring the mechanism directly, and it should be the
  number this candidate is budgeted against. What M18 *added* in C3's favour is
  Dayan §6.1's argument that some topologies are unreachable by any order of
  sequential shortest-path routing, so a post-pass is structurally necessary rather
  than merely nice.

* **Rejected at M19, 2026-08-24, with its numbers.** The owner declined the
  candidate on the re-priced figure rather than deferring it again. The trade, laid
  out:

  | | Value |
  |---|---|
  | Expected gain | **~3.4 % of wire length** — Dayan's ROAR optimiser, ten two-layer bins, 427 branches, detour 8.84 % → 5.18 %. A primary measurement of this exact mechanism. |
  | Runtime budget | **+30 % of corpus `router_seconds`**, which is what the historical measurement cost |
  | Superseded figure | The 7–16 % this candidate was carried at is Blake's, on **two** boards, second-hand |
  | Named customer | **Gone.** The `route-doubles-back` retrace on `examples/pcie-sata` was fixed at M17b by the via pass, for **+1.3 %** runtime on that board rather than +30 % of the corpus |
  | Corroborating evidence against | **53 of 55** collapsible spans corpus-wide were unimprovable at M17a — 21 because the on-layer route is longer, 32 because the free space is split, **zero on capacity** |
  | What survives in its favour | Dayan §6.1: some topologies are unreachable by *any* order of sequential shortest-path routing, so a post-pass is structurally necessary rather than merely nice |

  **3.4 % for +30 % fails any budget this project has stated.** Every candidate
  since M16 has been held to a runtime ceiling declared before it was built, and no
  ceiling in the record is anywhere near an order of magnitude of runtime per
  percent of copper. Dayan's §6.1 argument is accepted and does not rescue the
  candidate: it establishes that *something* post-convergence is needed for
  completeness on some topologies, not that **this** post-pass is worth 30 % of the
  corpus's routing time. That argument is the one thing that could reopen C3 — a
  cheaper mechanism addressing the same structural gap would be a new candidate,
  budgeted afresh, and not this one.

  **Where the number came from, so it is not re-derived wrongly:**
  [`routing-literature.md` §1.3](routing-literature.md#13-dayans-thesis-obtained--what-the-second-hand-markers-got-right-and-wrong).
  The rejection is recorded here rather than in a deleted branch precisely so the
  next person to propose a detour pass meets 3.4 % before they meet 7–16 %.

### C4. Route to the net, not to the pad

* **Technique** — replace the up-front Euclidean MST with cluster-to-cluster routing:
  seed the search from every terminal of the source group, terminate on any terminal
  of the destination group, and **merge the two groups when a connection completes**,
  so later connections of the same net can land anywhere on what is already connected.
* **Source** — observed mechanism (`cluster_create`, `cluster_merge`,
  `closest_cluster_pair`); the cluster formulation is Dayan/SURF's. Blake independently
  named EMST as the limitation this replaces, in his GSoC wrap-up.
* **Touches** — [`plan.py`](../../src/aipcb/route/plan.py) (`spanning_routes` becomes
  dynamic), [`graph.py`](../../src/aipcb/route/graph.py) (multi-source, multi-target
  search — `Terminal` becomes a set), `stretch.py` (`open_pads` becomes "the copper
  this route may land on").
* **Benefit** — shorter copper, better completion in congestion, and it removes the
  `copper_sliver` at its cause rather than trimming it afterwards.
* **Size** — large, and the most invasive candidate here. It changes what a
  "connection" *is*, which reaches the source format's `RouteTopology` (a declared
  sketch names two pads) and therefore the manual-routing path. **Probably needs its
  own ADR**, because it partially reverses a decision rather than extending one.

### C5. Order the first pass by pairwise conflict

* **Technique** — instead of ordering the first negotiation pass by priority and a
  difficulty heuristic, route each connection alone against an empty board, score each
  pair by how much their solo routes conflict, and derive the order from the resulting
  matrix.
* **Source** — observed mechanism (`netscore_pairwise_*`, `order_nets_preroute_greedy`).
* **Touches** — [`negotiate.py`](../../src/aipcb/route/negotiate.py) ordering only.
* **Benefit** — plausibly fewer negotiation iterations to converge. **Lowest confidence
  of the six**: aipcb negotiates, and negotiation exists precisely to make the first
  order matter less. It is listed because it is cheap to *measure* — count iterations
  to convergence on `examples/congestion` under both orderings — and the measurement
  settles it either way.
* **Size** — small, and it must not disturb `default_priority`: priority is what the
  *source* gets to say, and this must remain a tie-break beneath it.

### C6. A routing benchmark harness

* **Technique** — a fixture that routes a parametrically-scaled board at several sizes
  and records wall time, iterations to convergence, peak memory, and completion rate,
  as a committed table that a report can cite.
* **Source** — the autopsy. Runtime is what actually made the toporouter unusable, and
  it had no benchmark, so nobody could see it coming or tell a regression from a hard
  board.
* **Touches** — `tests/` (a benchmark that is *reported*, not asserted — a threshold
  here would be flaky), plus a section in the next report.
* **Benefit** — it discharges one of the three
  [graduation conditions](../roadmap.md#maturity-and-graduation) autorouting must meet
  to leave beta, and it is the prerequisite for evaluating C1–C4 honestly. **Do this
  one first**: three of the five candidates above trade runtime for quality, and
  without a baseline none of those trades can be judged.
* **Size** — small.

---

### Flagged exposures

These are not features. They are places where §B found aipcb sharing a weakness with
the toporouter and nothing watching, and where the fix is a test or an invariant.

#### E1. The capacity model under-counts cuts

`check_capacity` can pass a board Maley's criterion rejects (§A.6). Legality is
guarded downstream, so this is a diagnosis and completeness defect rather than a
correctness one — but the *check* claims more than it delivers, and the docstring in
[`field.py`](../../src/aipcb/route/field.py) asserts the criterion without the
qualification.

**Now, independent of whether C2 is built:** amend the `field.py` docstring and
`check.py::check_capacity`'s wording to say that the cut set is the triangulation's
diagonals and that this is a *lower bound* on congestion, not the criterion in full.
A check that overstates its own guarantee is worse than one that states its limits.

#### E2. Nothing checks that a net does not cross itself

[`crossing_nets`](../../src/aipcb/route/invariant.py) skips same-net pairs by design,
and it must — two connections of one net are *supposed* to meet. But that also means
nothing anywhere asks whether a *single leg* is a simple polyline, or whether two legs
of one connection overlap on one layer. The toporouter's tightener produced exactly
this class of geometry (its arc-loop checks exist for no other reason), and the aipcb
construction that could produce it — concatenating several funnel outputs across via
hops — is not covered by the argument that protects a single leg.

**Now:** add to `invariant.py` a check that (a) every leg's polyline is simple, and
(b) two legs of the *same connection* on the *same layer* do not overlap except at
shared endpoints. Both are one Shapely call. Expected result on the current corpus is
zero findings, which is the point: it is cheap insurance against the failure class
that cost the toporouter's author a hundred hours.

#### E3. Scale robustness is untested

Not a defect found, a premise unverified — and precisely the kind
[CLAUDE.md](../../CLAUDE.md) exists to catch. The toporouter was killed by a
coordinate-range change breaking a CDT's floating-point predicates. aipcb delegates its
CDT to GEOS and snaps to 1e-6 mm, which is almost certainly fine at board scale, but
"almost certainly" is the state the ADR-premise rule is about.

**Now:** one test that routes an existing example translated a long way from the
origin and scaled, and asserts the same topology comes out. If it passes, the premise
is measured and dated. If it does not, this note has paid for itself.

---

## Measured results, as the closure rule requires

The [part-2 rule](../roadmap.md#part-2-the-quality-candidates-and-the-rule-they-are-held-to)
says a candidate that does not pay for itself is recorded here rather than deleted.
Nothing in M17 was rejected outright — all three of its candidates came in under
budget — but three of its measurements are negative results and belong here, because
the next person to propose the same thing should meet them first.

**Via minimisation is nearly exhausted, and the 37/90 proxy overstates it.** The M16
baseline found 37 of 90 corpus layer changes made by connections that never met a
corridor above half capacity, and the roadmap said out loud that half capacity is a
convention and "no pressure anywhere on the connection" is not "no pressure at the
via". M17a sharpened it by asking the geometry instead. Of 55 candidate spans across
the eleven examples — every place a route leaves a layer and returns to it, plus
every connection whose two pads share a layer — **two collapse**. Twenty-one are
rejected because the single-layer route exists and is longer (so the via bought
something), and thirty-two because the free space on the target layer is genuinely
split in two. The 37 was a proxy; the geometry's answer is four vias, and they are
on one board.

**The capacity arbiter never fired.** M16a's special cuts were built so that a via
collapse could be tested against a sound cut model, and the model is wired in and
tested — but on this corpus **not one span was rejected on capacity**. Length is the
binding constraint everywhere here. That does not make the arbiter unnecessary (a
denser board is exactly where it would speak), it makes it currently unexercised by
real data, and the report says so rather than claiming a win it did not earn.

**The stretcher's cost was constant factors, not the algorithm.** M17c's profile
found that two thirds of the router's profiled wall clock on the largest board was not tightening at
all: it was the *field builder* being rebuilt from scratch for every connection the
shared field could not place, and inside it a scalar Shapely loop over tens of
thousands of candidate via sites. Batching those calls halved the corpus routing
time with **every board's output hash unchanged**. The algorithmic question — whether
the funnel-per-connection-against-a-whole-board-triangulation shape is asymptotically
right — was not touched and is M18's to answer.

**And one candidate is now rejected outright — C3, the first on the list to be.**
Not on a failed implementation: on a re-priced expectation. M18 obtained Dayan's
thesis and replaced the second-hand 7–16 % with a primary **3.4 %**, against a
**+30 %** runtime budget, after M17 had already removed the candidate's named
customer. The owner declined it on 2026-08-24 and the full numbers are in
[C3](#c3-a-detour-pass--rejected-at-m19) above. It is restated here because this is
the section the closure rule points at, and a rejection recorded only inside the
candidate that carried it is one the next reader has to already be looking for.

---

## What this changes

Nothing today: no code moved. What it leaves behind is a measured position on six
techniques, three of which (C1, C3, C4) are the "better layout" track and one of which
(C6) is a precondition for judging them; a soundness gap in the capacity model that
was not previously known (C2/E1); and two cheap guards (E2, E3) against failure
classes with hard historical evidence behind them.

*Written before M16. Since then C6 was built (M16c's bench harness), E1 and E2 were
closed, C3 was **rejected** on its re-priced numbers, and the "better layout" track
is down to C1 and C4 — with [M20](../milestones/m20-placement-quality.md)'s thesis
being that the rest of that track may not be in the router at all. The candidate
entries above carry their own current status; this paragraph is left as written so
the position it records stays datable.*

And the sustainability lesson, which is the part that does not fit in a candidate
list. The toporouter's algorithm was not what failed. What failed was that one
person's undocumented understanding was load-bearing, and when it went, a working
research result became a file nobody could safely change. This project's ADRs,
reports, invariants and re-measurement rule are the countermeasure to exactly that,
and the toporouter is the evidence for what happens without them.

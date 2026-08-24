# Routing literature: tightening theory, congestion lineage, modern methods

A study note. **Nothing in `src/`, `tests/` or `examples/` changed in the session
that produced it.**

[M16](../reports/m16.md) built the benchmark. [M17](../reports/m17.md) spent the
evidence it produced and halved the corpus's routing time by removing constant
factors — and left one question open, in its own words:

> With the per-point Shapely traffic gone, the largest single remaining cost is
> `geometry_for`: union the obstacles, difference them from the board, triangulate
> the result — once per connection per layer, from scratch, on a free space that
> differs from the previous connection's by one route's worth of copper. Nothing in
> this project knows whether that is avoidable.

That is the question this note exists to answer, and the answer is **yes, and the
canonical system did it in 1997**. Everything else here is secondary to that.

The method is [the toporouter postmortem](toporouter-postmortem.md)'s: per
technique, what it is in this note's own words with a citation → which aipcb
component it touches → expected gain against a measured baseline → runtime and
complexity risk → verdict, candidate or rejected. **Gaps in the record are marked
as gaps.** A source that could not be reached is a finding; a plausible summary of
a paper nobody opened is not.

---

## The licence wall, restated

Academic papers are the sources throughout, and no source code was copied from
anywhere. Where an implementation is named — Shewchuk's `Triangle`, Kallmann's
CDT, GEOS — it is named as an *address*, so a reader can find the thing being
described. Any candidate here that is ever built is built from the papers cited and
from this note. The same discipline the toporouter study set, for the same reason.

And one rule from [CLAUDE.md](../../CLAUDE.md) applies to the papers as well as to
tools: **a claim in a paper has a date on it.** Where a paper says an algorithm is
impractical, or a vendor says its router is three times better, that is recorded
here with its date and its provenance and is not promoted to fact.

---

## Sources, and what could not be got

| Source | Status |
|---|---|
| Hershberger & Snoeyink, *Computing minimum length paths of a given homotopy class* — WADS 1991; Computational Geometry: Theory and Applications 4(2):63–97, 1994 | **Read in full text.** Author's PostScript, `cs.unc.edu/~snoeyink/papers/homotopy.ps.gz`, dated 1995-05-19. Theorems 3.2, 3.4 and 4.4 are quoted below from it. The ScienceDirect copy is paywalled (HTTP 403); the author's copy is not. |
| Bespamyatnikh, *Computing Homotopic Shortest Paths in the Plane* — SODA 2003 / J. Algorithms | **Read in full text**, `personal.utdallas.edu/~besp/abs/homo.pdf`. Its introduction is the best short survey of the bounds in this family, and it is the source for the NP-hardness statement in §1.1. |
| Shewchuk & Brown, *Fast segment insertion and incremental construction of constrained Delaunay triangulations* — Computational Geometry 48(8):554–574, 2015 | **Read** (abstract and §1 in full; the proofs were not needed), `people.eecs.berkeley.edu/~jrs/papers/inccdtj.pdf`. |
| Kallmann, Bieri & Thalmann, *Fully Dynamic Constrained Delaunay Triangulations* — in *Geometric Modeling for Scientific Visualization*, Springer, 2003 | **Read in full text** (4 pages), EPFL Infoscience. The measured insert/remove timings in §1.6 are Table 1 of that paper. |
| **Dayan, *Rubber-Band Based Topological Router*, PhD dissertation, UC Santa Cruz, June 1997** | **Obtained in full text — 161 pages.** This closes [the toporouter note's largest gap](toporouter-postmortem.md#sources-and-what-could-not-be-got). Not from ProQuest and not from UCSC: from an Internet Archive snapshot (2017-07-18) of a Semantic Scholar PDF that the live site no longer serves. **The live routes are all still closed** — ProQuest wants an institution, Semantic Scholar returns an empty page, Google Books has metadata only — so the second attempt succeeded by archaeology rather than by access, and the copy could disappear again. §1.3 records what it says, and every second-hand marker in the toporouter note that it settles. |
| Dai, Dayan & Staepelaere, *Topological Routing in SURF: Generating a Rubber-Band Sketch* — DAC 1991 | **Not read.** ACM DL only. Its content is now available at first hand through the thesis instead, which supersedes the need. |
| Lu, *Dynamic constrained Delaunay triangulation and application to multichip module layout*, MSc thesis, UCSC, Dec 1991 | **Not obtained. Recorded as a gap.** This is the incremental CDT that SURF actually ran on (thesis reference [42]). No online copy was found. What it did is known only through one sentence of Dayan's thesis, quoted in §1.3. Shewchuk & Brown 2015 and Kallmann et al. 2003 are the obtainable modern equivalents and are what a candidate would be built from. |
| **TopoR / Eremex — published algorithm papers** | **None found. Recorded as a gap, and the search is described so the next person can do better.** Searched for peer-reviewed work by S. Yu. Luzin and O. B. Polubasov (the named FreeStyleTeam authors), for Eremex patents, and in the Russian EDA literature. What exists is: a Russian-language textbook (Luzin, Lyachek & Polubasov, *Printed circuit design automation: TopoR topological router*, SPbGETU "LETI", 2005, 163 pp — **not obtained**), a trade article (below), and a 2018 comparative-benchmark paper by other authors that measures TopoR without describing it. **No algorithmic account of TopoR's tightening or force relaxation was located in any language.** |
| Luzin & Polubasov, *Advantages of Isotropic PCB Routing*, Printed Circuit Design & Fab #6, Feb 2009 | **Read in full text**, from a vendor mirror. It is advocacy, not an algorithm paper: it argues *why* any-angle arc routing is better, and contains no description of how TopoR computes anything. Its quantitative claim — "reduce the total wire length by 25–40% and make the number of vias 2–3 times less" — carries **no benchmark, no baseline system and no method**, and is recorded here as a vendor claim rather than as a measurement. |
| EDN, *Speed and improve PCB routing* | **Unreachable.** Two fetch attempts timed out. Believed to be the same trade material; the gap is not thought to be material given the article above was read. |
| Chazelle, *A theorem on polygon cutting with applications*, FOCS 1982; *Triangulating a simple polygon in linear time*, Discrete & Computational Geometry 6:485–524, 1991 | **Not read directly.** Both are cited here at one remove — the first from Hershberger & Snoeyink's bibliography and from `funnel.py`'s own docstring, the second from Shewchuk & Brown 2015 §1, which is also the source of the practicality judgement quoted in §1.2. Marked as second-hand where it matters. |
| Leiserson & Maley, *Algorithms for routing and testing routability of planar VLSI layouts*, STOC 1985; Maley, *Single-Layer Wire Routing and Compaction*, MIT Press 1990 | **Not read in this session.** Already cited by [ADR 0006](../decisions/0006-routing-approach.md) and [ADR 0014](../decisions/0014-special-cuts.md); nothing here rests on a new claim about them. |

The congestion-lineage and machine-learning sources are listed in §2 and §3 with
their own reachability status, because the pattern there is different: those
literatures are largely open, and what limits them is relevance rather than access.

---

## The measurements this note rests on

Three numbers were measured on 2026-08-24, at HEAD `4a41546`, on the development
machine (16 cores, 30 GB RAM, Linux 7.0.0-29-generic), CPython 3.14.4, Shapely
2.1.2 / GEOS 3.13.1. They were taken with scratch scripts **outside** the
repository — M17's precedent — so nothing under `src/` gained an instrument.

They are here because a literature survey that recommends work without knowing
where the time is would be exactly the mistake [CLAUDE.md](../../CLAUDE.md) is
about, and because M17 §3.4 named `geometry_for` as the largest remaining cost
without publishing the post-M17c figure.

### M1. Where the seconds are now, on `examples/pcie-sata`

`cProfile` over one un-instrumented run. 37.4 s profiled in total, of which the
router is **29.8 s** and the build-and-parse ahead of it is 8.3 s.

**Read these as profiled seconds, not wall clock, and read the caveat before the
table.** The same call unprofiled takes **19.3 s** wall on this machine, against
37.4 s under `cProfile`, over 92.6 million calls — so the instrument roughly doubles
the run, and it does not do so evenly. Per-call overhead lands on functions called
millions of times and barely touches a handful of calls into GEOS. Everything in the
Python column below is therefore an **upper** bound on its real share, and everything
that is a single call into GEOS (`constrained_delaunay_triangles`, `union_all`,
`distance`, `covers`) is a **lower** bound. Where a conclusion below depends on
which side of that line a cost falls, it is stated. The committed baseline's own
figure for this board, unprofiled, is `router_seconds` **16.196 s**, of which the
`tighten` stage is **15.035 s** — 93 %.

| | cumulative | tottime | calls |
|---|---:|---:|---:|
| `_retry` → `_repair` (the private-field rebuild) | **16.30 s** | — | 16 |
| `field.py::build_field` | 14.34 s | — | 17 |
| `field.py::_via_sites` | 12.35 s | 0.78 s | 17 |
| `geometry.py::geometry_for` | **10.71 s** | — | 176 |
| `triangulate.py::triangulate_free` | 9.77 s | 0.46 s | 244 |
| `triangulate.py::locate_many` | 7.97 s | **2.80 s** | 68 |
| `shapely::constrained_delaunay_triangles` | 4.30 s | **4.30 s** | 244 |
| `triangulate.py::_inside` | 3.59 s | **2.00 s** | 3 140 252 |
| `field.py::_covered` | 2.65 s | — | 68 |
| `triangulate.py::free_space` | 2.28 s | — | 244 |
| `shapely::distance` | 1.47 s | 1.47 s | 794 |
| `shapely::union_all` | 1.32 s | 1.31 s | 273 |
| `negotiate.py::negotiate` (**the whole negotiation**) | 1.25 s | — | 1 |
| `triangulate.py::_sign` | 1.07 s | 1.07 s | 9 426 354 |
| `graph.py::search_path` (**the whole multilayer A\***) | 2.05 s | 0.57 s | 84 |
| `triangulate.py::portal_path` | 0.43 s | 0.23 s | 2 443 |
| `stretch.py::stretch_guided` | 0.83 s | 0.03 s | 867 |
| **`funnel.py::tighten` — the funnel algorithm itself** | **0.039 s** | 0.019 s | 823 |

### M2. The funnel costs 0.13 % of routing

That last row is the finding of this whole survey and it deserves to be said
plainly. **The tightening algorithm — the thing three milestones have called the
hot spot — is thirty-nine profiled milliseconds.** Unprofiled it is less. The
committed baseline puts the `tighten` *stage* at 15.035 s on this board; the funnel
inside it is four hundredths of a second, and the profiling caveat above only makes
that gap wider, because `tighten` is one of the functions whose 823 calls the
instrument barely touches. Add every function
that touches the sketch-to-geometry conversion (`portal_path`, `reduce_crossings`,
`orient_portals`, `tighten`, `_dedupe`, `stretch_guided`) and it is still under one
second.

The `tighten` *stage* is 83–94 % of routing, as M16 measured, and that is not
wrong. But almost none of that stage is tightening. It is **building the free space
and the triangulation and the via sites that the tightening is done against** — and
then throwing them away and building them again for the next connection.

### M3. How much of the free space actually changes between rebuilds

`geometry_for` was wrapped from outside and asked, per call: how many obstacles it
was given, how many of those are new since the previous call *for the same layer*,
how many vanished, how big the resulting triangulation is, and how many distinct
`(clearance, track_width)` rule pairs the layer sees across the run.

| Board | Layer | calls | obstacles | triangles | new per call, median | max | disappearances | distinct rule pairs |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **pcie-sata** | F.Cu | 137 | 14 → 516 | 683 → 1093 | **2** | 46 | 84 | 6 |
| | B.Cu | 39 | 0 → 501 | 192 → 268 | **9** | 118 | 12 | 4 |
| **mcu-4layer** | F.Cu | 56 | 0 → 160 | 414 → 655 | **1** | 31 | 8 | 3 |
| | B.Cu | 8 | 81 → 160 | 204 → 357 | **6** | 41 | 0 | 1 |
| **qfn-fanout** | F.Cu | 24 | 0 → 176 | 484 → 686 | **4** | 37 | 0 | 2 |
| | B.Cu | 30 | 0 → 176 | 381 → 519 | **4** | 25 | 0 | 2 |

Read the F.Cu row of `pcie-sata` slowly. **One hundred and thirty-seven times, a
triangulation of a thousand triangles is built from scratch because a median of two
obstacles out of five hundred changed.**

Two caveats that a candidate has to survive, and they are the reason this
measurement was taken rather than assumed:

* **The sequence is not monotone.** Eighty-four obstacles *disappear* from F.Cu's
  input across the run — the repair pass gives a connection a different obstacle
  set, the via pass rebuilds after a collapse, a pair's coupled geometry differs
  from its members'. So insertion-only incrementality is not enough; either the
  deletion half exists too, or a rebuild stays as the fallback for the case.
* **The obstacle *geometry* differs between calls, not just the obstacle list.**
  Each obstacle is inflated by `max(own clearance, other clearance) + width/2`, so
  the same copper is a different polygon for a different net class. Six distinct
  rule pairs on `pcie-sata`'s F.Cu means an incremental structure has to be keyed
  per rule pair — six maintained triangulations on that layer rather than one. That
  is still six structures against 137 rebuilds, but it is not free, and any budget
  that assumes one structure per layer is wrong.

---

## 1. Tightening theory

### 1.1 Hershberger & Snoeyink: the funnel, and what is actually proved about it

**What it is.** Given a triangulated surface and a path α drawn on it, lift α into
the *universal covering space* — the unrolled surface in which the path's winding
around obstacles becomes a plain sequence rather than a topological fact. In that
unrolled space, the homotopy class of α is a *sleeve* of triangles, and the sleeve
is a topological disc. Sweep it one diagonal at a time, maintaining an apex and two
concave chains; whenever the chains cross, the crossing vertex becomes a corner of
the answer and the new apex. The unifying idea of the paper is that the universal
cover lets one algorithm serve four different problems (Euclidean shortest path,
minimum-link path, shortest loops, restricted orientations) — and that it does so
using "simple arrays and stacks" instead of the finger-search trees earlier work
needed.

The paper is careful that it did not invent the funnel: it attributes it to
Chazelle (1982) and Lee & Preparata (1984), which is the same attribution
`funnel.py` already carries.

**The bounds, quoted.** Define *C*<sub>α</sub> as the complexity of the input path
(the number of pieces composing it) and Δ<sub>α</sub> as the number of times α
crosses a triangulation edge. Then:

> **Theorem 3.2** The Euclidean shortest path that is homotopic to a given path α
> can be computed in *O*(*C*<sub>α</sub> + Δ<sub>α</sub>) time and |*U*<sub>α</sub>|
> space.

> **Theorem 3.4** One can trace a path α through the universal cover of a BTM and
> maintain the funnel in *O*(*C*<sub>α</sub> + Δ<sub>α</sub>) time and space.

Two things follow, and they point in opposite directions.

**First: there is no asymptotic improvement available.** Theorem 3.2 is linear in
the size of the sleeve, and *that is exactly what `funnel.py` claims and does*. The
`O(n log n + nk)` figure [ADR 0006](../decisions/0006-routing-approach.md) records
is the cost of the whole pipeline including triangulation, not of the sweep. When
Bespamyatnikh (2003) writes that Hershberger & Snoeyink's algorithm is "optimal in
the worst case assuming that the running time is evaluated in terms of input
parameters *n* and *k*<sub>in</sub>", the word *optimal* is doing real work: it is a
matching lower bound, not a "best known". Later results in this family —
Bespamyatnikh's own *O*(*n* log *n* + *k*) and Efrat, Kobourov & Lubiw's
*O*(*n*<sup>3/2</sup> + *k* log *n*), both read — improve the case where **many**
paths are tightened against **one static** obstacle set by making the per-path cost
output-sensitive after a shared preprocessing step. None of them makes one path
against one triangulation cheaper than linear in its sleeve.

Set that against measurement **M2**: aipcb's funnel is 0.13 % of routing.
Multiplying zero by a constant factor is still zero.

**Second: Theorem 3.4 is the one nobody in this project has read before.** The
funnel can be *maintained* as the path is extended, on-line, in the same time
bound. It is not a batch algorithm that must be re-run from the start when its
input changes at one end. That is the theoretical licence for incremental
re-tightening, and it is the shape of the answer §1.6 arrives at from a completely
different direction.

**One further fact worth having on the record**, because it justifies the whole
architecture rather than any candidate. Hershberger & Snoeyink, §1.1, and
Bespamyatnikh, §1, both state it: routing wires with fixed terminals among fixed
obstacles is tractable *when a sketch is given* — when each wire's homotopy class is
specified — and

> "When no sketch is given or when the terminals are not fixed, the resulting
> problems are usually NP-hard."

aipcb's split between `negotiate.py` ("negotiation is symbolic") and `stretch.py`
("topology in, DRC-clean geometry out") is not a stylistic preference. It is the
line between a polynomial problem and an NP-hard one, and it was arrived at
independently in [ADR 0006](../decisions/0006-routing-approach.md). Nothing in the
literature suggests moving it.

* **Touches** — [`funnel.py`](../../src/aipcb/route/funnel.py), and nothing else.
* **Verdict — no candidate.** The shipping funnel is the algorithm the canonical
  paper proves optimal, and it costs 39 ms. **This is the direct answer to the
  M17c question, for option (a): algorithm replacement in the tightener is
  rejected on measurement, not on argument.**

### 1.2 Chazelle: already in the code, and the part that is not worth having

**What it is.** Two separate results are meant when people say "Chazelle" in this
context, and they have different verdicts.

*A theorem on polygon cutting with applications* (FOCS 1982) is one of the four
independent discoveries of the funnel algorithm. It is **already the citation in
`funnel.py`'s docstring**, alongside Tompa (1981), Lee & Preparata (1984) and
Leiserson & Maley (1985). There is nothing here to import; it is imported.

*Triangulating a simple polygon in linear time* (Discrete & Computational Geometry
6:485–524, 1991) is the celebrated *O*(*n*) triangulation. Two things disqualify it
from the free-space problem, and both are cited rather than reasoned:

1. **It is for simple polygons.** aipcb's free space is a polygon with holes —
   every pad, every via, every finished track is a hole — and the linear-time result
   does not extend to that case. Bespamyatnikh (2003) §2, faced with the same
   obstacle-laden setting, does not reach for Chazelle's linear-time algorithm; he
   uses Bar-Yehuda & Chazelle's *O*(*n* log<sup>1+ε</sup> *n* + *k*<sub>in</sub>).
2. **Shewchuk & Brown (2015) §1 record the practical judgement**, and it is dated:
   Chazelle's algorithm is "celebrated as a theoretical breakthrough but is
   considered too complicated for practical use". That is a 2015 statement by
   somebody who ships a triangulation library. Under the CLAUDE.md rule it is a
   claim with a date, not a fact — but it is the best-sourced judgement available
   and nothing found in this survey contradicts it.

* **Touches** — nothing.
* **Verdict — rejected, reasoned.** The useful half is already in the code; the
  famous half does not apply to a polygon with holes and is recorded as impractical
  by the person best placed to say so. Re-open only if somebody ships a
  linear-time triangulator for polygons with holes that is faster than GEOS in
  practice, which is a much narrower claim than "Chazelle exists".

### 1.3 Dayan's thesis, obtained — what the second-hand markers got right and wrong

The toporouter postmortem says of this source: *"Not obtained in full text… Claims
below about 'what the thesis prescribes' are therefore weaker than claims about
what the code does, and are marked where they appear."* Those markers can now be
resolved. **The thesis was obtained in full (161 pages) from a 2017 Internet
Archive snapshot** — see the source table for why that is archaeology rather than
access.

**What it is.** A multi-layer topological detailed router, part of the SURF system
at UCSC. Four steps: layer assignment, net ordering, sequential embedding, and a
rip-up-and-reroute wire-length optimiser. The interconnect is represented as a
**rubber-band sketch** (RBS) throughout, and the thesis's own summary of its
contribution is worth quoting because it is precisely the claim the postmortem
could only report at second hand:

> "A mathematical formulation of the concept of RBS is also presented and is used
> to prove the correctness of the shortest-path algorithm. This is the first exact
> analysis of RBS ever published."

**Finding 1 — the shortest-path algorithm is superlinear, and the one that ships
is not shortest.** From the conclusion, verbatim:

> "The algorithm finds a shortest planar path in *O*((*T*+*S*)² log(*T*+*S*)) time
> by searching in the region visibility graph. By considering only a planar subset
> of the edges of the graph, the algorithm is guaranteed to find in
> *O*((*T*+*S*) log(*T*+*S*)) time a planar path (if one exists), that is likely to
> be short."

(*T* is terminals, *S* is net segments. The superscript is reconstructed from the
PDF's layout; §5.5 states the same pair of bounds and the contrast between them is
unambiguous either way.) §5.5 is blunter still: the reduced-graph algorithm "is
guaranteed to find a planar path if one exists but the path found may be not a
shortest path". The planar subset is obtained by **triangulating the terminals** and
keeping only graph edges that correspond to a triangulation edge.

This corrects a mild over-reading in the postmortem, which pairs "an algorithm with
a correctness proof in a thesis" against "a maximum-violation-plus-recursion
heuristic in the shipped code". The proof is real and it is Theorem 5 — but the
*shipping* SURF router used the fast approximation, and Dayan says so.

**Against aipcb.** The comparison is favourable and is worth stating precisely.
Dayan searches for the path *in the rubber-band sketch itself*, so search and
geometry are one step and the exact version is quadratic-ish in terminals plus
segments. aipcb separates them: A\* over the triangulation with a priced cost model
([`graph.py`](../../src/aipcb/route/graph.py), 2.05 s on `pcie-sata`), then a
funnel that is linear in the sleeve (0.039 s). aipcb's decomposition is not a
weaker version of Dayan's; it is a different and cheaper one, and the funnel
half of it has a matching lower bound behind it (§1.1) that Dayan's region
visibility graph does not.

**Finding 2 — SURF maintained its triangulation incrementally, and said why.**
§2.2.1, verbatim:

> "To improve the efficiency of rubber-band updating, each layer of the sketch is
> built on top of a triangular mesh. This mesh is maintained using an incremental
> constrained Delaunay triangulation algorithm. In addition to providing incremental
> triangulation modifications, it supports efficient geometrical queries such as
> point location, nearest neighbor, and range search. Since the size of the
> triangulation is linear in the number of terminals in the sketch, this data
> structure is more space efficient than a traditional grid-graph."

**That is M17c's open question, answered by the original system, in 1997, in one
sentence.** The union-difference-triangulate that aipcb does 176 times per board is
not what the canonical rubber-band router did. It maintained one mesh per layer and
modified it. The algorithm it used is thesis reference [42], Yizhi Lu's 1991 UCSC
master's thesis on dynamic CDT — **which could not be obtained, and is recorded in
the source table as a gap.** What can be obtained is the modern equivalent, and §1.6
is built on that instead.

**Finding 3 — the detour pass has a primary measurement now, and it is smaller than
the second-hand one.** [Candidate C3](toporouter-postmortem.md#c3-a-detour-pass--rejected-at-m19)
currently cites Blake's before/after numbers, 7–16 % less copper on two historical
boards. Dayan's chapter 6 measures the same idea — his ROAR operator — on ten
two-layer bins, 427 branches, and reports it as *detour*, the excess over the sum
of each net's independently-optimal length:

| | Detour before | Detour after | Improvement |
|---|---:|---:|---:|
| Average over 10 bins | 8.84 % | 5.18 % | 3.67 points |

> "This implies that the wire length was reduced by about 3.4 % and that about 38 %
> of the detour length was eliminated."

Per-bin improvement ranges from 0.04 to 8.44 points. **3.4 % average wire length is
the number a detour pass should be budgeted against, not 7–16 %** — Dayan is the
primary source, measuring the mechanism directly, on ten cases rather than two. It
also agrees with what M17 found by accident: 53 of 55 collapsible spans on aipcb's
corpus could not be improved at all.

**Finding 4 — why a post-pass is structurally necessary, not merely nice.** §6.1
gives the argument, and it is one this project has not made anywhere:

> "it overcomes an inherent limitation of sequential routing of the nets on a
> shortest path that in some cases, no order of the nets will result in an optimal
> solution."

His example is the *triangle problem*: three symmetrical branches whose optimal
embedding cannot be produced by routing one at a time along shortest paths, in any
order. This is a real argument for C3 and against [C5, pairwise-conflict
ordering](toporouter-postmortem.md#c5-order-the-first-pass-by-pairwise-conflict) —
**no ordering can fix what ordering cannot fix.** C5's own confidence was already
the lowest of the six; this is independent evidence pointing the same way.

**Finding 5 — Dayan's net ordering is the same idea as C5, and he says what it is
worth.** §4.5 formulates 2-net ordering as an optimisation over *ordered pairwise
conflicts*, exactly C5's mechanism, and §4.6 leaves the complexity question open:
"the question of the complexity of the 22NOP and the 2NOP is still open". His claim
for it is comparative and modest — it "results in shorter wiring than the 'shortest
first' approach" — and note that the baseline it beats is *shortest-first*, not
negotiated congestion. aipcb does not use shortest-first.

* **Touches** — the findings touch [`plan.py`](../../src/aipcb/route/plan.py) (C3's
  home), [`negotiate.py`](../../src/aipcb/route/negotiate.py) (C5), and the
  postmortem note's own text, which now has a first-hand source where it had a
  second-hand one.
* **Verdict — no new candidate; three existing ones are re-priced.** C3's expected
  gain drops from 7–16 % to **~3.4 %** and gains a structural justification. C5
  loses further ground. And Finding 2 becomes the backbone of §1.6.

### 1.4 TopoR / Eremex: a gap, and what the gap is made of

**What was looked for**: published algorithmic accounts of TopoR's arc-based
tightening and force relaxation, by the named authors (S. Yu. Luzin, O. B.
Polubasov and the FreeStyleTeam), in the EDA literature, in patents, and in Russian
sources.

**What was found**: a trade article, read in full, which argues for any-angle
routing with smoothed wires on grounds of space utilisation (a circle's area is
less than that of a circumscribed polygon), etch and soldering quality at sharp
corners, board warp from globally uniform layer directions, and via inductance —
and which contains **no description of any algorithm**. Its headline claim, "reduce
the total wire length by 25–40 % and make the number of vias 2–3 times less",
appears with no benchmark suite, no baseline system, and no method. A Russian
textbook by the same authors exists and was **not obtained**. A 2018 comparative
paper by other authors benchmarks TopoR against Specctra without describing either.

**This is a gap in the record and is left as one.** The toporouter note's sources
were the starting point the milestone brief suggested; they did not lead anywhere
new. Concretely, the two things worth knowing — how TopoR relaxes a topology into
arcs, and whether its force relaxation has a termination argument — remain unknown.
If somebody later reaches the 2005 LETI textbook, that is where to look.

* **Verdict — no candidate, for want of a source.** The one thing the trade article
  does supply is corroboration, from a shipping commercial router, of the argument
  [the postmortem makes against arc output](toporouter-postmortem.md#b4-why-nobody-fixed-it):
  arcs are a real quality feature and not a vanity. That does not change the
  postmortem's conclusion — if arcs are ever built here, they belong in a separate,
  independently testable post-pass over finished polylines, not inside the
  tightener — but it does mean the idea should not be dismissed, only sequenced.

### 1.5 Incremental and dynamic constrained Delaunay triangulation

This is the literature that answers the question M17c asked, and it is not in the
shortest-path family at all.

**What it is.** A CDT is normally built by triangulating the vertices and then
inserting the constraining segments one at a time; to insert a segment you delete
every edge and triangle its interior crosses, then re-triangulate the two cavities
it leaves. Shewchuk & Brown (2015) give a randomised algorithm that does one such
insertion in **expected time linear in the number of edges the segment crosses** —
*O*(*m*), where *m* is the number of triangles the segment passes through — where
the commonly implemented method is *O*(*m*²). Their abstract states the whole-CDT
consequence:

> "A result of Agarwal, Arge, and Yi implies that randomized incremental
> construction of CDTs by our segment insertion algorithm takes expected
> *O*(*n* log *n* + *n* log² *k*) time. We show that this bound is tight by
> deriving a matching lower bound."

They also state the practitioner's case for the approach, which matters more here
than the exponent: *O*(*n* log *n*) CDT algorithms exist (Chew; Seidel) but "Seidel's
algorithm has not been implemented, and Chew's algorithm has been implemented only
once… perhaps because they are complicated", whereas incremental segment insertion
"is easier to program and competitive in practice", and its pseudocode "we turned
into working C code in five hours".

**Shewchuk & Brown does not do deletion.** For that, Kallmann, Bieri & Thalmann
(2003) is the source: insertion *and removal* of constraints, with degeneracies
(overlapping edges, self-intersections, duplicated points) detected and fixed
on line, giving what they call a *fully dynamic* CDT. Their measured timings, Table
1, on a Pentium III 600 MHz:

| Data set | CDT size | Full construction | Constraint | Removal | Insertion |
|---|---:|---:|---:|---:|---:|
| head | 1 433 v | 2.1 s | 8 v | 3 ms | 4 ms |
| hexagons | 8 332 v | 3.7 s | 6 v | 3 ms | 4 ms |
| world map | 80 652 v | 38.1 s | 60 v | 50 ms | 20 ms |

The **ratio** is the transferable part, not the absolute times, which are from a
2003 machine. Rebuilding the world map costs 38.1 s; moving one 60-vertex constraint
inside it costs 70 ms. That is a factor of roughly 540. On the smaller sets the
ratio is larger still.

**Against aipcb.** [`triangulate.py::triangulate_free`](../../src/aipcb/route/triangulate.py)
calls `shapely.constrained_delaunay_triangles` on the differenced free-space
polygon and gets back a flat list of triangles — **there is no persistent mesh at
all.** The `Triangulation` object is derived from that list and thrown away with it.
Measurement M1 says that call is 4.30 s of a 29.8 s route, and measurement M3 says
the median call differs from its predecessor by two obstacles out of five hundred.

The obstacle to adopting this is not the algorithm, it is that **aipcb does not own
a mesh data structure**. GEOS gives triangles, not adjacency; the adjacency
(`_link`) is rebuilt every time from sorted endpoint keys. A dynamic CDT needs the
half-edge or SymEdge structure Kallmann et al. spend a section on, and building one
means owning a floating-point in-circle predicate — which is **exactly the thing
that killed the toporouter** ([postmortem §B.1](toporouter-postmortem.md#b1-what-actually-broke):
nested `swap_if_in_circle` calls ten thousand deep after somebody changed the
coordinate units) and exactly what [ADR 0006](../decisions/0006-routing-approach.md)
refused to write. Kallmann et al. report the same hazard in their own words — "we
have faced never-ending loops during the flipping process when using only a simple
determinant evaluation" — and their answer is an epsilon, which is the weaker of
the two known answers (the other being exact predicates).

* **Touches** — [`triangulate.py`](../../src/aipcb/route/triangulate.py) most of
  all, [`geometry.py::geometry_for`](../../src/aipcb/route/geometry.py),
  [`field.py::build_field`](../../src/aipcb/route/field.py) and `_via_sites`.
* **Expected gain** — bounded above by M1: `triangulate_free` 9.77 s and
  `geometry_for` 10.71 s of a 29.8 s route, so a *perfect* incremental scheme is
  worth at most about a third of routing on the largest board. Realistically less,
  because M3's six distinct rule pairs mean several maintained meshes per layer and
  because eighty-four deletions have to be handled.
* **Runtime and complexity risk — high, and of a specific named kind.** This is the
  one candidate in this note that would put a floating-point geometric predicate
  back inside this project, against ADR 0006's explicit refusal and against the
  toporouter's cause of death. It is also large: a mesh with adjacency, insertion,
  deletion, and a point-location structure, replacing a single GEOS call.
* **Verdict — candidate, scoped, and deliberately staged behind a cheaper one.**
  See [L1](#l1-an-incremental-free-space-mesh) in the candidate list, and §1.6 for
  why it is second in line rather than first.

### 1.6 The answer to M17c's question

The milestone asked, concretely: is the right next move **(a)** algorithm
replacement with a named algorithm, **(b)** incremental or lazy re-tightening, or
**(c)** a compiled kernel of the current algorithm?

**(a) Algorithm replacement — rejected, on measurement.** Hershberger & Snoeyink
Theorem 3.2 gives *O*(*C*<sub>α</sub> + Δ<sub>α</sub>) for tightening a path in a
given homotopy class, Bespamyatnikh records that bound as worst-case optimal, and
that is what `funnel.py` already implements. Measurement M2 closes it from the other
side: the funnel is 0.039 s of 29.8 s. There is no algorithm to replace it with and
nothing to win if there were. **This question is settled and should not be asked
again.**

**(c) A compiled kernel — rejected as posed, and one narrow piece of it accepted.**
"Compile the current algorithm" is the wrong shape, because the largest single cost
in M1 is `constrained_delaunay_triangles` at 4.30 s of pure GEOS: it is C already,
and a numba or Rust rewrite of the Python around it cannot touch it. But M1 also
shows something M17c missed. `locate_many` is 7.97 s cumulative, and underneath it
`_inside` runs **3 140 252 times** for 2.00 s and `_sign` **9 426 354 times** for
1.07 s. M17c batched the R-tree query and left the exact point-in-triangle test as a
scalar Python loop. That is roughly **6 s of 30** in one predicate that numpy can do
on whole arrays — the same move M17c made for `_covered`, applied to the tail it did
not reach. It is not a compiled kernel; it is one more day of the engineering M17c
was doing, and **the correct reading of M1 is that constant-factor work is not
exhausted after all.**

**(b) Incremental re-tightening — accepted as the structural direction, with the
order inverted.** Four independent sources agree:

1. Hershberger & Snoeyink Theorem 3.4: the funnel can be *maintained* on-line, in
   the same bound, rather than recomputed.
2. Dayan §2.2.1: SURF maintained a per-layer mesh with an incremental CDT,
   explicitly "to improve the efficiency of rubber-band updating".
3. Shewchuk & Brown 2015 and Kallmann et al. 2003: the update is local, expected
   linear in what the change touches, and measured at two to three orders of
   magnitude cheaper than reconstruction.
4. aipcb's own measurement M3: the median rebuild is provoked by two obstacles out
   of five hundred.

But **"incremental re-tightening" is the wrong noun.** Nothing needs to re-tighten:
the tightening is free. What needs to stop being rebuilt is the *free space, its
triangulation, and the via sites derived from it* — 16.30 s of `_repair` plus
10.71 s of `geometry_for`, against 0.039 s of funnel.

And there is a cheaper version of the same idea that comes first. M1 says the
single largest cumulative cost is not `geometry_for` at all: it is **`_repair`, at
16.30 s for sixteen connections** — a whole `LayeredField` for the entire board,
built from scratch, because one connection's negotiated path would not tighten. Of
that, `_via_sites` is 12.35 s. A private field is built for *one* connection and
then discarded, and the via sites it derives cover the whole board when the
connection needs a pocket near its own route. **Bounding the private field to the
region the failed connection can reach is ordinary engineering with no new
geometry**, no predicate, and no ADR to reverse — and it addresses more than half
the profile.

So the answer is **(b), staged**:

| | | expected gain | risk |
|---|---|---|---|
| **first** | vectorise the point-location tail (`_inside`/`_sign`) | ~6 s of 30 on `pcie-sata` | very low — M17c's own pattern, output must not move |
| **then** | bound the private repair field to the failing connection's region | up to 16.3 s of 30; realistically a large fraction | low-medium — changes what the repair searches, so results can move; needs the golden files to hold or the diff explained |
| **then, and only then** | an incremental free-space mesh replacing per-connection triangulation | at most ~10 s of 30, less after M3's caveats | **high** — a hand-owned CDT with floating-point predicates, against ADR 0006 and against the toporouter's cause of death |

That ordering is the whole recommendation. The literature says the third row is
possible and that the canonical system did it; the profile says the first two rows
are larger, cheaper and safer; and the CLAUDE.md rule says to measure the thing in
front of you before designing around somebody else's paper.

---

## 2. The negotiated-congestion lineage

**Priority, stated up front and not softened.** Negotiation costs **1.25 s** on
`pcie-sata` (M1) and **at most half a second** on every other board in the corpus.
Ten of eleven examples converge in one iteration; `enclosure` takes four. **Nothing
in this section is a speed candidate, and nothing in it can justify itself on the
current corpus** — by construction, since there is nothing for a better cost
function to improve when the first pass is already legal. What is mined here is
*quality* technique for the boards that do not yet exist: the congestion stress
example the [graduation conditions](../roadmap.md#maturity-and-graduation) require,
and the externally-contributed boards that will follow it.

Every claim below is from a paper that was read in full text unless the source
table says otherwise.

| Source | Status |
|---|---|
| Cho & Pan, *BoxRouter: A New Global Router Based on Box Expansion and Progressive ILP*, DAC 2006 | Read in full text (authors' copy, UT Austin CERC). |
| Cho, Lu, Yuan & Pan, *BoxRouter 2.0: A Hybrid and Robust Global Router with Layer Assignment for Routability*, ACM TODAES 14(2) art. 32, 2009 | Read in full text (authors' copy). |
| Chang, Lee & Wang, *NTHU-Route 2.0: A Fast and Stable Global Router*, ICCAD 2008 | Read in full text. Its §II.B restates NTHU-Route 1.0's cost function verbatim, which is how 1.0 is cited here — **1.0's own ASP-DAC 2008 paper was not read**. |
| Chang et al., *NTHU-Route 2.0: A Robust Global Router for Modern Designs*, IEEE TCAD 2010 | **Paywalled — not read.** |
| Pan & Chu, *FastRoute*, ICCAD 2006; *FastRoute 2.0*, ASP-DAC 2007; Zhang, Xu & Chu, *FastRoute 3.0*, ICCAD 2008; Xu, Zhang & Chu, *FastRoute 4.0*, ASP-DAC 2009 | All four read in full text, via Internet Archive captures of the authors' page (the live URLs now 404). |
| Pan, Xu, Zhang & Chu, *FastRoute: An Efficient and High-Quality Global Router*, VLSI Design 2012, art. 608362 | Read in full text (open access, via an Archive capture of the Hindawi PDF). |
| **Dai, Liu & Li, *NCTU-GR*, IEEE TVLSI 20(3), 2012; Liu, Kao, Li & Chao, *NCTU-GR 2.0*, IEEE TCAD 32(5), 2013** | **Paywalled — not read, and this is a real gap.** IEEE only; no author copy on either author's page; Semantic Scholar reports the open-access status as CLOSED; the project page ships a stripped binary and no paper. What is recorded below about NCTU-GR is **abstract-level and artefact-level only** and is labelled as such. In particular **its "two-stage cost function" is named in the abstract and its formula is not known here.** |
| Moffitt, Roy & Markov, *The Coming of Age of (Academic) Global Routing*, ISPD 2008 (invited) | Read in full text via an Archive capture. |
| Nam, Sze & Yildiz, *The ISPD Global Routing Benchmark Suite*, ISPD 2008 | **Not read** — ACM interstitial. |

### 2.1 What the lineage disagrees about, which is the finding

There are two branches, and they contradict each other on the central question.

**Branch one keeps PathFinder's history term and stabilises it.** BoxRouter 2.0's
A\* cost is `cost_i(e) = h_i(e) + α·p(e) + β·d(e)`, with present congestion
*exponential* in utilisation, `p(e) = P^(U(e)/C(e))`, and history *additive with a
growing increment*, `h_i(e) = h_{i−1}(e) + i` when the edge is over capacity.
NTHU-Route's is `cost_g = b_g + h_g × p_g + vc_g` — history **multiplies** present
rather than being added to it — with `p_g = ((d_g+1)/c_g)^5` and `h_g += 1` per
overflowing iteration.

**Branch two rejects the history term outright.** FastRoute's journal paper says so
in as many words:

> "such negotiation-based cost adjustment lacks theoretical basis and requires
> significant tuning before it can work properly. Instead of negotiation based maze
> routing technique, we propose virtual capacity, a systematic alternative"

and moves the accumulated state **out of the cost and into the capacity**:
`vc_e = c_e − max(0, p_e − c_e)` to start, then `vc_e ← vc_e − o_e` with
`o_e = u_e − c_e` each iteration. Because `o_e` is allowed to be *negative*, an edge
that stops being congested gets its virtual capacity back — a decay mechanism that
additive history does not have. FastRoute 3.0 damps the recovery asymmetrically at
`F = 0.85`, because "the virtual capacity reduction procedure is irreversible and
the capacity is continually lost".

**And every paper in branch one had to add a stabiliser.** BoxRouter 2.0 observed
its own router "spin out of control": once history dominates present cost, "a
presently congested edge becomes cheaper to pass through than a previously congested
edge", and overflow starts *rising* with more iterations. Its fix is a dynamic
scaling factor, `α = max_e[h_i(e)] / P`, which keeps a presently-full edge as
expensive as a previously-full one. NTHU-Route 2.0 bounds its history amplifier at
`k2/(k2−1) = 3`. That is three independent groups arriving at the same failure mode.

### 2.2 Against aipcb — including one place aipcb is already ahead

[`negotiate.py`](../../src/aipcb/route/negotiate.py) and
[`costs.py`](../../src/aipcb/route/costs.py) implement PathFinder directly. Per gate
move, [`graph.py`](../../src/aipcb/route/graph.py) charges

```
step  +  (congestion_cap if occupancy > 1 else step × present × occupancy)
      +  history[edge]  +  direction penalty
```

with `present = 0.8 × 1.9^iteration`, `congestion_cap = 1e6`, and
`age()` adding `congestion_history × overuse` — **4 mm times `used/capacity − 1`** —
to each over-subscribed cut per iteration.

**The instability BoxRouter 2.0 found cannot happen here, and the reason is
structural rather than lucky.** aipcb's over-capacity cost is a hard cliff at 1e6,
not an exponential; history grows by at most `4 × overuse` mm over at most twelve
iterations. History cannot come within five orders of magnitude of the cliff, so a
presently-full cut can never become more attractive than a formerly-full one. That
is worth writing down because the same three papers say it is the failure mode
everybody hits.

**And on the one point where all three read papers have the same gap, aipcb is
already on the right side of it.** BoxRouter 2.0's history increment is `+i`
(adaptive to *time*); NTHU-Route's is `+1` (constant); neither is adaptive to *how
much* an edge overflows by. aipcb's is `history_mm × overuse` — **proportional to
the overflow, in millimetres**. In a real-valued capacity model that is the obvious
right answer, and none of the grid papers does it, because on an integer grid you
overflow by one net or by two and the distinction barely exists.

### 2.3 Technique by technique

**T1. Virtual capacity in place of a history term — FastRoute 3.0/4.1.**
*What it is:* stop making a contested cut cost more; make it *be* narrower.
Initialise `vc_e` below `c_e` where a congestion estimate predicts overflow, then
subtract the realised overflow (which may be negative) each iteration, damped on
recovery.
*Touches:* [`field.py`](../../src/aipcb/route/field.py)'s `capacity`/`history`
arrays and `age()`, [`costs.py`](../../src/aipcb/route/costs.py).
*Expected gain:* the strongest single-technique ablation in the whole survey —
FastRoute 4.1 without virtual-capacity adjustment solves **5 of 16** ISPD08
benchmarks overflow-free instead of 12, at **11.3×** the runtime. On aipcb's corpus,
zero: nothing is contested for long enough to matter.
*Risk:* low-medium. It is arithmetic on an array aipcb already owns, and
`fits()` — the hard geometric filter — must keep using the *real* capacity, or the
router will refuse corridors that physically hold a track.
*Verdict:* **candidate, scoped, and gated on a board that needs it.** This is the
best fit of anything in §2, for a reason that is specific rather than aesthetic:
aipcb's capacity is already a real number in millimetres, so virtual capacity is
dimensionally identical to a field that exists, whereas a history term is a
dimensionless quantity that has to be rebalanced against a cost scale.

**T2. BoxRouter 2.0's dynamic α.** *Rejected, reasoned* — see §2.2. The failure it
fixes cannot occur against a hard cliff. **Re-open if the cliff is ever softened**,
which T1 would do, because virtual capacity and a 1e6 cliff are alternatives rather
than companions.

**T3. NTHU-Route 2.0's Gompertz base-cost anneal**, `b_g = 1 − e^(−β·e^(−γ·i))`
with β = 5, γ = 0.1: early iterations price wirelength normally, late iterations
price it at nearly zero so the router will detour arbitrarily far to clear an
overflow. *Touches* `costs.py` only; it is a pure function of the iteration index
and carries nothing grid-specific. *Expected gain:* nothing measurable here — it
only acts from iteration three or four onward and ten of eleven boards stop at one.
*Verdict:* **candidate, scoped, low priority.** File it against the congestion
stress board. It is perhaps twenty lines.

**T4. Multi-source multi-sink search over the partial tree** — FastRoute 2.0's, with
an optimality proof by super-source/super-sink reduction. Seed Dijkstra from *every*
point of one subtree and stop at the first point of the other.
*Touches:* [`graph.py::search_path`](../../src/aipcb/route/graph.py) (`Terminal`
becomes a set), [`plan.py::spanning_routes`](../../src/aipcb/route/plan.py).
*Verdict:* **not a new candidate — this is [C4](toporouter-postmortem.md#c4-route-to-the-net-not-to-the-pad)
arriving from a second direction**, and that is the useful finding. C4 was proposed
from the toporouter's cluster merging and Dayan's formulation; FastRoute reaches the
same mechanism from ISPD contest work, states an optimality proof for it, and names
the three pathologies it removes — unnecessary detour, redundant routing over a path
the tree already has, and unintentional loops. The middle one is exactly aipcb's
`copper_sliver`. Two independent lineages converging on one mechanism raises C4's
confidence; nothing in either lowers its cost, which stays large.

**T5. Rip-up ordering: largest region first, outermost congestion first**
(NTHU-Route 2.0). Both are geometric arguments — a large net has more freedom, and
congestion is radially distributed so freeing the outer ring releases what the inner
nets need. *Touches* `negotiate.py::_losers` ordering only. *Verdict:* **candidate,
small, and it is the cheapest thing in §2 to try** — but it needs a board where
more than one iteration happens, so it is gated on the same stress example as T1.

**T6. Adaptive expanding search region**, growing with iteration and congestion,
capped (FastRoute caps at 20 % of the graph). A CDT lives in the plane, so
restricting the A\* to cuts inside an inflated bounding region is natural.
*Verdict:* **rejected for now, reasoned** — this is a *speed* technique for graphs
with millions of nets, and `search_path` costs 2.05 s profiled on the largest board
in this corpus. It buys nothing until a board is large enough for the search to
matter, and it can only make results worse before then.

**T7. Layer assignment as a separate stage.** BoxRouter 2.0 does it by ILP, minimising
via span; FastRoute 4.x by a spiral DP over a via grid graph. *Verdict:* **rejected,
reasoned.** aipcb decides layer, via position and corridor in *one* minimisation
([`graph.py`](../../src/aipcb/route/graph.py)'s docstring says why: "that is the
difference between a router that *may* use another layer and one that *chooses*
to"). Splitting that into two stages would be a reversal, not an extension, and the
grid routers do it because their 3-D graph is otherwise too large — a pressure aipcb
does not have at four layers. Two fragments are portable if the question ever
re-opens and are recorded so the search is not repeated: FastRoute's ordering rules
(nets by increasing Σwirelength/pins, segments by increasing distance to a pin so
one end's layer range is always known), and BoxRouter 2.0's warning that inter-layer
spacing must be constant or vias at different depths acquire different implicit
weights.

**T8. Monotonic routing** (FastRoute 2.0, NTHU-Route). *Rejected, and the literature
itself is split.* The O(mn) dynamic program depends on the partial order a
rectilinear bounding box induces on grid points; a CDT has no such order and no
translation is offered by any of these papers. Note also that Moffitt, Roy & Markov
(ISPD 2008 §6.2) argue monotonic routing "may be ineffective because [it imposes]
restrictions on the freedom of the router" and that boxed A\* "is guaranteed to find
solutions that are as good or better" — while FastRoute 2.0's and NTHU-Route 2.0's
own ablations report it helping. **That disagreement is unresolved in the literature
and is recorded as unresolved.** It does not matter here, because the technique does
not port at all.

**T9. Edge shifting, node shifting, Hanan-grid warping, ACE, progressive ILP,
3-bend/L/Z pattern routing, bend-as-via-proxy costing.** *All rejected, one reason
each, all grid-specific:*
* *Edge/node shifting* slide a rectilinear Steiner segment along its "safe range"
  **without changing wirelength** — a fact about the rectilinear metric. Under
  Euclidean length, moving a Steiner point almost always changes the length, so the
  free-move premise evaporates.
* *Hanan warping* rests on the Hanan theorem, which Moffitt, Roy & Markov say is
  "not true even in the approximate sense" once costs are non-uniform — *on grids*.
  Their transferable recommendation instead is a congestion-weighted MST with
  continual restructuring under rip-up, which needs no Steiner machinery. aipcb
  already builds a Euclidean MST; making its weights congestion-aware is a small
  variant and is folded into C4 rather than listed separately.
* *ACE* iterates over the rows and columns of a rectangular bounding box. There are
  no rows or columns in a triangulation. Its *goal* — a better-than-probabilistic
  initial usage estimate to seed T1 — transfers; its algorithm does not, and a
  replacement would have to be invented rather than ported.
* *Progressive ILP* has two binary variables per wire, one per L-shape.
* *3-bend and pattern routing* are defined by counts of rectilinear bends.
* *NTHU-Route 2.0's via cost* `vc_g = v_g × c_g × b_g` prices a **2-D bend** as a
  statistical proxy for vias, on the assumption of preferred layer directions
  (⌈19/9⌉ = 3 expected vias per bend on six layers). aipcb has explicit vias with an
  explicit `via_cost_mm`; a bend proxy would be strictly worse information.

**T10. One numerical hazard to carry forward if T1 is ever built.** Every trigger in
these papers is an integer comparison — `U(e) > C(e)`, where U and C are counts of
nets. In a real-valued model, `used − capacity` can be 10⁻⁶ mm, and an exponential
cost is at its most sensitive exactly there. Any port needs a real tolerance on the
overflow test, and `k`/`S` slope constants must be dimensioned per millimetre or
normalised by capacity — otherwise the same constant behaves completely differently
on a 0.4 mm cut and a 12 mm one. This is not a candidate; it is a note for whoever
writes the milestone.

### 2.4 What is missing from this section, said plainly

NCTU-GR and NCTU-GR 2.0 were **not read**. Both are IEEE-only, neither author
publishes a copy, and the project page ships a binary. From the abstracts, NCTU-GR
contributes circular fixed-ordering monotonic routing, an evolution-based rip-up
and reroute with a **two-stage cost function**, and via minimisation by layer
shifting and reassignment; NCTU-GR 2.0 contributes bounded-length maze routing and a
task-based multithreaded parallel router. **The two-stage cost function is exactly
what this section was mining for, and its formula is not known here.** From the
released binary's parameter file — an artefact rather than a paper — the tool offers
three interchangeable layer-assignment algorithms including a negotiation-based one,
and a single scalar `Wirelength_Optimization_Level`. Nothing further should be
inferred from that.

Two of the parallel-routing ideas in NCTU-GR 2.0 would in any case run straight into
this project's determinism requirement; see §3.

---

## 3. Modern and machine-learning routing

The standing hypothesis was that non-determinism disqualifies this family from the
core. **It survives, and the literature supplies the grounds rather than this
project's priors.** But the survey forces one correction to how the objection is
stated, and it turns up something better than the thing it was sent to look for.

| Source | Status |
|---|---|
| Liao et al., *A Deep Reinforcement Learning Approach for Global Routing*, ASME JMD 142(6):061701, 2020 (arXiv:1906.08809) | Read in full text. |
| Liao et al., *Attention Routing: Track-Assignment Detailed Routing Using Attention-Based Reinforcement Learning*, ASME IDETC/CIE 2020 (arXiv:2004.09473) | Read in full text. |
| Liao et al., *Track-Assignment Detailed Routing Using Attention-based Policy Model With Supervision*, MLCAD 2020 (arXiv:2010.13702) | Abstract read in full; body by keyword search only. |
| Song et al., *PCBWorld: A Benchmark Environment for Engine-Grounded PCB Design Automation*, KDD Agentic AI Workshop 2026 (arXiv:2607.05915v2) | Read in full text. |
| Cheng, Kahng, Kundu, Wang & Wang, *An Updated Assessment of Reinforcement Learning for Macro Placement*, arXiv:2302.11014v3 (10 Mar 2026) | Read in full text. |
| Markov, *Reevaluating Google's Reinforcement Learning for IC Macro Placement*, CACM Nov 2024 (arXiv:2306.09633v8) | Read in full text. |
| Goldie, Mirhoseini & Dean, *That Chip Has Sailed*, arXiv:2411.10053 | Abstract and introduction read in full. |
| Mirhoseini et al., *A graph placement methodology for fast chip design*, Nature 594:207–212, 2021 | **Paywalled — body not read.** Abstract and the full editorial history were read from the journal page. |
| Liu et al., *FastGR: Global Routing on CPU–GPU*, IEEE TCAD 42(7), 2023 | Read in full text. |
| Lin, Xiao, Liu & Young, *InstantGR: Scalable GPU Parallelization for Global Routing*, ICCAD 2024 | Read in full text. |
| Zhao, Guo, Zhang & Lin, *GAP-LA*, arXiv:2507.13375v2 | Read in full text. |
| Fan et al., *gCDT: A Highly Parallel GPU Algorithm for Large-Scale Constrained Delaunay Triangulation*, SIGGRAPH 2026 | Read in full text (author preprint). |
| **Livesu, Cherchi, Scateni & Attene, *Deterministic Linear Time Constrained Triangulation using Simplified Earcut*, IEEE TVCG 2022 (arXiv:2009.04294v2)** | Read in full text. Surfaced from gCDT's bibliography and turns out to matter for [L1](#l1-an-incremental-free-space-mesh), not for this section. |
| He, Agarwal, Yang, Manohar & Pingali, *SPRoute 2.0: A Detailed-Routability-Driven **Deterministic** Parallel Global Router with Soft Capacity*, ASP-DAC 2022 | Read in full text. |
| Shen, Zhang, Luo & Xiao, *Serial-Equivalent Static and Dynamic Parallel Routing for FPGAs*, IEEE TCAD 39(2), 2020 | Read in full text. |
| Greco & Baier, *Bounded-Suboptimal Search with Learned Heuristics*, PRL Workshop 2021 | Abstract and introduction read in full. |
| Yan, Lyu, Cheng & Lin, *Towards Machine Learning for Placement and Routing in Chip Design: a Methodological Overview*, arXiv:2202.13564 | Full text retrieved and word-searched. |
| NVIDIA, *Floating Point and IEEE 754 Compliance for NVIDIA GPUs* | Read. |
| Lin, Liu & Wong, *GAMER: GPU Accelerated Maze Routing*, ICCAD 2021 | **Paywalled — abstract only.** Five candidate open mirrors 404'd. Determinism unknown. |
| He et al., *Circuit Routing Using Monte Carlo Tree Search and Deep RL*, VLSI-DAT 2022 | **Unreachable — abstract only.** The NSF public-access mirror refused connections; IEEE paywalled. |
| Gandhi et al., *Applying reinforcement learning to learn best net to rip and re-route in global routing*, ACM TODAES 2024 | **Paywalled — abstract only.** Architecturally the most interesting of the unread ones; see §3.4. |
| Xie et al., *RouteNet*, ICCAD 2018 | **Paywalled — abstract only.** |
| DeepPCB (InstaDeep), `deeppcb.ai` | Homepage read. **No peer-reviewed publication located** after searching arXiv and Semantic Scholar. |

### 3.1 "DeepRoute" does not exist in this field

The name in the brief is a conflation, and saying so is the finding. Three
unrelated things carry it: an autonomous-driving company; a 2019 *network* traffic
engineering paper; and, by association, **DeepPCB** — InstaDeep's commercial cloud
PCB router, for which **no technical publication was found at all**. Its site claims
"96 % mean completion", "DRC-clean by default", "up to 8 layers, 1200 connections",
"all in minutes, not hours". Those are marketing claims with no benchmark, no
baseline router and no method, and they are recorded here as marketing claims.
Nothing about its internals is asserted here, because there is no non-marketing
source to assert it from.

### 3.2 What the RL routing literature actually reports

**The evaluation protocol is the argument.** PCBWorld (2026) is the most relevant
paper in this section by a distance — it is built on **KiCad 9.0.8**, uses native
`.kicad_pcb` files, benchmarks against Freerouting on 679 real open-source boards,
and is therefore aimed at exactly this project's problem. Its default reporting
protocol, verbatim:

> "For each board we draw five rollouts and select the single rollout with the
> largest potential gain… We write **@5** for this best-of-five protocol, the
> default for all reported metrics."

> "Freerouting and the RL agents report the mean over 4 seeds."

**A field whose default metric is best-of-five-rollouts, seed-averaged, cannot hand
a technique to a tool whose CI asserts a byte-identical board file.** Note the
second line especially: even *Freerouting*, a rule-based router, is treated as
seed-dependent — which is independent corroboration of the reason
[external autorouters are rejected, not deferred](../roadmap.md#rejected-not-deferred).

**Non-determinism here is structural, not a settable flag.** Liao et al.'s DQN
selects actions ε-greedily **at solve time**, with ε = 0.05 — a coin flip on one
step in twenty of the finished route, not merely during training. And it is a
*per-instance* optimiser: "the Q-network gets updated iteratively over many cycles
on the same target problem", at roughly an hour of GPU training per 8×8×2 board.
Cheng et al. document the harder version of the same problem in Google's Circuit
Training:

> "Despite non-determinism of CT training and its outcomes (even with the same
> seed, environment, and machine)…"

with a figure showing two runs on the same netlist, same machine, same environment
and the same default seed 333, one converging and one not.

**And the quality case is not made.** PCBWorld's own abstract: PCB routing is a task
where "learning-based methods still lag behind rule-based routers", with Freerouting
beating PPO on the larger real-board split. Liao's Attention Routing: the genetic
router it replaces "performs better in almost all problems" — the win is 100× speed
at worse quality. Cheng et al.: simulated annealing and human baselines beat
AlphaChip using "substantially fewer resources", and "no successful reproduction by
others of claims in [Nature] has been published… as of November 2025". Markov
records that at the MLCAD 2023 macro-placement contest, "top six teams used
traditional analytical optimization methods sans ML". Google's rebuttal disputes the
methodology of the reproduction; **neither side disputes that the system is
non-deterministic**, which is the only fact this project needs.

* **Verdict — rejected, reasoned, in the style of
  [Rejected, not deferred](../roadmap.md#rejected-not-deferred).** A learned policy
  that samples, that trains per instance, or that reports its results as a
  distribution over seeds breaks the determinism property `test_routing_is_byte_stable`
  exists to defend, and there is no numbered quality claim in this literature that
  would justify absorbing that. **The reopening conditions are in §3.5.**

### 3.3 GPU acceleration: fast, and three papers out of four never mention determinism

* **FastGR** (2023) gets 2.489× over CUGR with a task-conflict-graph scheduler. The
  words *determinism*, *reproducible*, *atomic* and *race* do not appear in it. Its
  architecture — deterministic conflict graph, deterministic batches, conflict-free
  parallel execution — is *compatible* with determinism, but the paper neither
  claims nor tests it, and it does report that "net ordering influences the final
  solution quality".
* **InstantGR** (ICCAD 2024) is faster still (2.01× over the ISPD 2024 winner on
  4 × A800) and **buys the speed by deliberately introducing a race**: its
  "representative point exhaustion" is explicitly *non-exact* overlap checking that
  works by "allowing a little bit of overlap… useful for routing algorithms that are
  insensitive to a little overlap". Nets that overlap can land in one batch and race
  on shared demand. That is a stated design decision to accept run-to-run variation.
* **GAP-LA** (2026): same conflict-batch shape, same silence.
* **NVIDIA's own floating-point document** is the reason none of this is incidental.
  Individual IEEE-754 operations reproduce between CPU and GPU; *reduction order*
  does not, and "changing the number of threads per block reorganizes the
  reduction" — so a tuning parameter changes the answer.

* **Verdict — rejected, reasoned, and doubly so.** Nothing in this corpus routes in
  seconds where aipcb takes minutes for a reason a GPU would fix; the profile (M1)
  says the cost is a serial GEOS pipeline, not a parallel search. And the field's
  leading result achieves its speed by explicitly tolerating non-reproducibility.

### 3.4 The one shape that survives — and nobody has run it here

The brief asked whether the literature offers a **learned heuristic inside a
deterministic search**, whose output can only order or guide a search that stays
correct without it. **It does, the theory is sound, and it has never been applied
to PCB routing with a determinism guarantee.**

The formal home is **Focal Search** (Greco & Baier 2021). It takes *two* heuristics:
an admissible `h` sorting OPEN, which is where the suboptimality bound comes from,
and an arbitrary `h_FOCAL` sorting FOCAL, which is where a learned model goes. Their
framing of why this is the right architecture is the whole argument:

> "learned heuristics, even if highly accurate, cannot be assumed to be admissible,
> preventing us from using well-known bounded-suboptimal algorithms such as
> Weighted A\*."

A wrong model costs time; it cannot cost correctness. And their empirical result is
a bonus: the variant that works best uses **only the ranking** between successors
rather than heuristic values — and a model that emits an ordering is exactly the
kind that can be frozen to integer output and made bit-stable.

Three published systems have the right shape without the guarantee. Liao's Attention
Routing defines a **"deterministic greedy rollout"** (argmax at each step) and uses
it as the REINFORCE baseline — the mechanism exists and is named in the paper — but
the paper never says which rollout is used at test time and never discusses
reproducibility. **RL-Ripper** (TODAES 2024) learns only *which net to rip up*,
leaving CUGR to route — architecturally ideal, and **paywalled, not read**, so
nothing about its numbers or seeding is claimed here. **RouteNet** is a pure
feed-forward CNN that *predicts* congestion and does not route — **paywalled, not
read**.

The gap is measurable and nobody has measured it. A dedicated methodological survey
of ML for placement and routing (Yan et al. 2022) was word-searched: `determinis`
**0** hits, `reproduc` **0**, `random seed` **0**, `stochastic` **0**. No paper
found in this survey reports whether a frozen, inference-only model reproduces
bit-exactly across runs, thread counts or machines. Under
[CLAUDE.md](../../CLAUDE.md)'s rule, that is precisely the measurement that would
have to exist before designing around it — and it does not.

Two further points of arithmetic against it, specific to this project. Liao's DQN's
own paper says the advantage exists only where capacity depletes mid-route: "With
admissible heuristics, which is the case in this research, A\* router is guaranteed
to yield the optimum solution for each two-pin problem". And aipcb's search costs
**2.05 s profiled** on its largest board (M1). A learned orderer would be optimising
7 % of the runtime, at the cost of shipping a model, a training pipeline, and a new
class of reproducibility risk.

* **Verdict — rejected, reasoned, with a named reopening condition (§3.5).** Not
  "ML is disqualified": *sampling, per-instance training, and reporting results as a
  distribution* are disqualified. A frozen ranker inside a deterministic search is
  not disqualified in principle. It is disqualified here because it would optimise
  the wrong 7 % of the profile and because the reproducibility measurement it would
  need has not been made by anybody.

### 3.5 Under what conditions this reopens

Written out so the next person proposing it meets an argument rather than a silence,
and so that the argument can be *checked* rather than re-litigated.

1. A paper runs a **frozen, inference-only, fixed-point-quantised** model as a
   **net-ordering or FOCAL-list ranker** over a deterministic router, **and measures
   bit-exact reproducibility** across runs, thread counts and machines. Model →
   ranking → deterministic search; no sampling in the inference path, no per-instance
   training, no floating-point reduction whose order can change.
2. It shows a quality or runtime win over a strong classical baseline **on real
   boards**. PCBWorld's D3-B and D3-C splits are now the obvious yardstick, and D3-C
   — hundreds of nets on real open-source boards — is explicitly unsolved by anyone.
3. The thing being sped up is something the profile says is worth speeding up. On
   today's profile that is not the search.

### 3.6 The unexpected finding: determinism under parallelism is a solved problem, by non-ML work

This is the part of §3 worth keeping, and it arrived while looking for something
else. If this project ever wants to use more than one core — and at 11 minutes for
an extrapolated 900-connection board it eventually will — the determinism question
arrives whether or not machine learning is anywhere near it. Two papers have
answered it.

**SPRoute 2.0** (ASP-DAC 2022) gets deterministic parallel global routing from
**bulk-synchronous batching**: every net in a batch reads the *same* demand
snapshot, buffers its changes, and the buffer is applied only at the batch boundary.

> "nets read the same usage and make the same path searching decisions regardless of
> the execution order in the batch, and generate deterministic solutions"

and, crucially, "region disjointness is not required to guarantee determinism" — you
never have to prove two nets cannot collide, only that they read the same snapshot.
The measured cost of the guarantee is **"< 0.1 %"** quality against the
non-deterministic version of the same router, for 4.3× maze-routing speedup on eight
threads. The two hazards they name in advance are load imbalance and **livelock**
(nets in a batch competing for the same resource so overflow stops falling), fixed
by shrinking batch size over iterations.

**Shen et al.** (IEEE TCAD 2020) get the stronger property and draw the distinction
this project should adopt in its vocabulary:

> "serial equivalency is completely different from determinism, and it is an even
> stronger constraint"

Determinism is *the same answer every run*. **Serial equivalency** is *the same
answer as the single-threaded router, whatever the core count* — achieved by staging
nets according to the serial order and running only independent ones concurrently.
19.13× on 32 cores on the VTR benchmarks. Their motivation reads as though written
for this repository's CI: routers that "cannot provide the deterministic results…
are impractical in an industrial context".

* **Touches** — nothing today. This is a design constraint recorded in advance, not
  a candidate.
* **Verdict — no candidate now, and a named design rule for later.** If parallelism
  is ever proposed here, **serial equivalency is the bar, not determinism**, because
  `test_routing_is_byte_stable` and the committed golden files pin the *serial*
  answer and a merely-deterministic parallel router would move all of them. The
  design to copy is bulk-synchronous batching with buffered demand updates; the
  hazard to plan for is livelock. And per CLAUDE.md, both papers' claims get
  re-measured against whatever the tools do at the time, not taken on trust from a
  2020 and a 2022 paper.

### 3.7 What §3 hands to §1

Two things, and they are the reason this section was worth writing even though its
headline verdict was predicted in advance.

**gCDT confirms the geometric foundation from the other direction.** Its GPU CDT
uses **fixed-precision integer arithmetic** because "integer arithmetic avoids
round-off error", and reports that this gives "deterministic Orient2D/InCircle
decisions on the GPU". aipcb already snaps every triangulation vertex to
`_QUANT = 1e-6` mm — nanometres, KiCad's own internal resolution — so its coordinates
are *already integers in disguise*. That materially lowers the risk on
[L1](#l1-an-incremental-free-space-mesh), which is otherwise the candidate that puts
a floating-point predicate back into this project. (gCDT's own *output* is
non-reproducible, and deliberately: it uses last-writer-wins atomics because "we do
not require deterministic selection". That is a choice it made for parallelism, not
a property of the predicates.)

**Livesu et al. (TVCG 2022) supply the deterministic half of segment insertion.**
Shewchuk & Brown's insertion is *expected* linear and *randomised*. Livesu et al.
prove that the cavity left behind by a segment insertion belongs to a restricted
class of simple polygons in which "all their convex vertices but two can be used to
form triangles in an earcut fashion, without the need to check whether other polygon
points are located within each ear" — giving an **optimal deterministic linear time**
re-triangulation that is "trivial to implement", with a correctness proof, and with
"all convexity checks performed with exact orientation predicates, hence the
algorithm is numerically robust". They report being faster than the randomised state
of the art on 3 969 of 4 408 test models.

For a project whose determinism is load-bearing and whose ADR 0006 refused to write
a CDT because hand-rolled ones are "a classic source of subtle, data-dependent
bugs", the combination — integer coordinates it already has, exact predicates, a
deterministic linear-time cavity fill with a proof — is a materially different risk
picture from the one that refusal was made against. **It does not reverse ADR 0006;
it is the evidence a future ADR would have to weigh.**

---

## 4. Candidates

Numbered **L**, so they do not collide with the toporouter note's C1–C6. Each is
scoped so a milestone prompt can be drafted from it directly. The drafted proposal
that selects among them is
[`m19-incremental-geometry.md`](../milestones/m19-incremental-geometry.md), approved
2026-08-24 from the draft this note's session wrote.

**Source discipline**, as in the postmortem: every one is to be implemented from
this note and the cited papers, never from anybody's source tree.

### L0. Vectorise the point-location tail

* **Technique** — `Triangulation.locate_many` batches the R-tree query and then
  falls back to a **scalar Python loop** over candidates, calling `_inside` (three
  `_sign` calls each) per candidate. M1: `_inside` **3 140 252 calls**, `_sign`
  **9 426 354 calls**. Replace the tail with a vectorised barycentric or
  three-orientation test over numpy arrays, keeping the tie-break — *ascending
  triangle index* — exactly, so a point on a shared edge still lands where it does
  today.
* **Source** — none. This is not a literature candidate; it is here because the
  literature review's own profiling found it, and because M17c's §3.2 established
  the pattern and stopped one function short of it.
* **Touches** — [`triangulate.py`](../../src/aipcb/route/triangulate.py) only.
* **Expected gain** — up to ~6 s of 30 profiled on `pcie-sata`, and **less than
  that unprofiled**: this is Python code with a huge call count, which is exactly
  what `cProfile` overstates (see the caveat above M1). Treat 6 s as a ceiling and
  anything above 10 % of `router_seconds` as a success.
* **Risk** — very low, and the *only* real risk is the tie-break. A vectorised
  `argmin` over a boolean mask does not naturally reproduce "lowest index wins";
  it has to be written to.
* **Verdict — candidate, scoped. Do it first.** Cheapest thing in this note, and
  it must not move a single board hash.

### L1. An incremental free-space mesh

* **Technique** — stop rebuilding. Maintain, per (layer, net-rule) pair, a
  persistent constrained-Delaunay mesh of free space with adjacency, and apply each
  newly-placed piece of copper as a **segment insertion** (delete the triangles the
  segment crosses, re-triangulate the two cavities) rather than re-running
  union → difference → triangulate over the whole board. Where copper is *removed*
  — 84 times on `pcie-sata`'s F.Cu (M3) — either remove the constraint or fall back
  to a rebuild.
* **Source** — Shewchuk & Brown 2015 (segment insertion in expected time linear in
  the edges crossed; randomised incremental CDT construction in expected
  *O*(*n* log *n* + *n* log² *k*), with a matching lower bound); **Livesu, Cherchi,
  Scateni & Attene 2022** (the cavity re-triangulation in *deterministic* linear
  time, with a correctness proof and exact orientation predicates); Kallmann, Bieri
  & Thalmann 2003 (constraint *removal*, and the measured update-versus-rebuild
  ratio); and Dayan 1997 §2.2.1, which is the same architecture in the original
  rubber-band router.
* **Touches** — [`triangulate.py`](../../src/aipcb/route/triangulate.py) (a mesh
  object with adjacency, insertion, deletion and point location, where today there
  is a list of triangles from one GEOS call),
  [`geometry.py::geometry_for`](../../src/aipcb/route/geometry.py),
  [`field.py::build_field`](../../src/aipcb/route/field.py) and `_via_sites` (which
  could then be maintained incrementally too — only the triangles that changed need
  new incentres).
* **Expected gain** — bounded above by M1: `geometry_for` 10.71 s and
  `triangulate_free` 9.77 s of a 29.8 s profiled route. Kallmann et al.'s measured
  rebuild:update ratio is ~540:1 on their largest set, which is the shape to expect
  rather than the number to promise.
* **Risk — high, and specifically named.** It puts a geometric predicate back into
  this project, against [ADR 0006](../decisions/0006-routing-approach.md)'s explicit
  refusal, and it is the exact failure that killed the toporouter. Three things have
  changed since that refusal and a future ADR would have to weigh them: aipcb
  already snaps coordinates to `_QUANT = 1e-6` mm, so it is *already* on an integer
  lattice, which gCDT (2026) reports as what makes orientation and in-circle
  decisions deterministic; Livesu et al. give a deterministic, provably-correct,
  exact-predicate cavity fill; and this project has the invariants and byte-stability
  tests the toporouter never had. Against that: M3's six rule pairs per layer mean
  several meshes, not one, and the deletions mean the sequence is not monotone.
* **Verdict — candidate, scoped, and deliberately *third*.** It is the structurally
  right answer and the literature is unanimous about it. It is also the largest and
  riskiest thing in this note, and L0 and L2 take a bigger bite for a fraction of
  the cost. **Do not start it before the two cheaper candidates have been measured**
  — if they land, L1's remaining prize is much smaller than it looks today.
* **Do first — measure.** Re-run M3 on the whole corpus, not three boards, and add
  one figure it does not have: how much of `geometry_for`'s cost is the GEOS
  `difference`/`union_all` (which an incremental mesh would *also* have to replace)
  versus `constrained_delaunay_triangles` alone. M1 says 4.30 s and 1.32 s
  respectively on one board; the split decides whether L1 is worth a third of
  routing or a seventh.

### L2. Bound the private repair field to the failing connection

* **Technique** — `_repair` builds a whole-board `LayeredField` for **one**
  connection, sixteen times on `pcie-sata`. Restrict the rebuilt field to the region
  that connection can plausibly use — its two pads' bounding box, inflated by a
  margin derived from the board's own dimensions — so the via-site derivation and
  the triangulation are over a fraction of the board.
* **Source** — none directly; the *idea* of a bounded search region is FastRoute's
  adaptive expanding box and BoxRouter's box expansion (§2, T6), used here for a
  different purpose. Rejected there as a search technique, useful here as a
  *construction* technique.
* **Touches** — [`plan.py::_repair`](../../src/aipcb/route/plan.py),
  [`field.py::build_field`](../../src/aipcb/route/field.py) (a bounding argument).
* **Expected gain** — `_repair` is **16.30 s of 29.8 s profiled**, the single
  largest cumulative cost in M1, and `_via_sites` is 12.35 s of it. A box covering a
  quarter of the board would be a large fraction of that.
* **Risk — low to medium, and it is a *quality* risk rather than a runtime one.**
  A repair that cannot see the whole board may fail where today it succeeds. The
  guard is the existing one: `_repair` returning `False` already falls through to a
  named handover. Expansion-on-failure (start small, grow, rebuild whole-board as
  the last attempt) keeps completion unchanged by construction.
* **Verdict — candidate, scoped. Do it second.** It is the largest measured cost in
  the profile, it needs no new geometry, and it reverses no decision.

### L3. Virtual capacity in place of the history term

Described in full at [§2.3 T1](#23-technique-by-technique). **Candidate, scoped,
gated** on a board that does not converge in one iteration — which does not exist in
this corpus yet and is one of the
[graduation conditions](../roadmap.md#maturity-and-graduation).

### L4. The Gompertz base-cost anneal, and rip-up ordering

[§2.3 T3 and T5](#23-technique-by-technique). Both small, both **candidates,
scoped**, both gated on the same missing board as L3. T5 is the cheaper of the two
to try.

### L5. Serial-equivalent parallelism — a design rule, not a candidate

[§3.6](#36-the-unexpected-finding-determinism-under-parallelism-is-a-solved-problem-by-non-ml-work).
Recorded so that if parallelism is ever proposed, the bar is **serial equivalency**
(same answer as the single-threaded router, whatever the core count) rather than
mere determinism, and the design to copy is bulk-synchronous batching with buffered
demand updates.

### Rejected, with reasons, so they are not proposed again

| | Why |
|---|---|
| **Replacing the funnel with a better shortest-homotopic-path algorithm** | Hershberger & Snoeyink Theorem 3.2 is *O*(*C*<sub>α</sub> + Δ<sub>α</sub>), matched by a lower bound, and is what `funnel.py` implements. The funnel costs **0.039 s** of a 30 s route (M2). §1.1. |
| **Chazelle's linear-time triangulation** | For *simple* polygons; aipcb's free space has holes. Recorded as "too complicated for practical use" by Shewchuk & Brown (2015). §1.2. |
| **BoxRouter 2.0's dynamic α** | Fixes an instability that cannot occur against aipcb's hard 1e6 over-capacity cliff. Re-opens only if L3 replaces that cliff. §2.2. |
| **Layer assignment as a separate stage** | aipcb decides layer, via and corridor in one minimisation on purpose; splitting it is a reversal. The grid routers split it because their 3-D graph is otherwise too large, which is not a pressure at four layers. §2.3 T7. |
| **Monotonic routing** | Depends on the partial order a rectilinear bounding box induces on grid points. No translation to a triangulation exists, and the literature is itself split on whether it helps. §2.3 T8. |
| **Edge/node shifting, Hanan warping, ACE, progressive ILP, pattern and 3-bend routing, bend-as-via-proxy costing** | Each depends on rectilinear geometry, integer capacities, or preferred layer directions. One reason each, §2.3 T9. |
| **Adaptive expanding search box (as a search technique)** | A speed technique for graphs with millions of nets. `search_path` is 2.05 s profiled here. §2.3 T6. Note L2 borrows the *shape* of the idea for a different job. |
| **RL / learned policy routing** | Non-determinism is structural, not a flag: ε-greedy action selection at solve time; per-instance training; results reported as best-of-*n* over seeds. And the quality case is not made — PCBWorld (2026) reports learning methods still behind rule-based routers on real boards. §3.2, reopening conditions §3.5. |
| **GPU-accelerated routing** | The profile says the cost is a serial GEOS pipeline, not a parallel search. And the state of the art buys its speed by explicitly accepting a race — InstantGR's non-exact overlap check is "useful for routing algorithms that are insensitive to a little overlap". §3.3. |
| **A learned heuristic inside a deterministic search** | The one shape that survives the determinism objection in principle, and it is rejected on two other grounds: it would optimise the 7 % of the profile that is search, and **no paper anywhere measures whether a frozen inference-only model reproduces bit-exactly**. §3.4. |

---

## 5. What this changes

**The three-milestone framing of the router's cost was wrong, and this note is
where that is written down.** M16 measured that 83–94 % of routing time is in the
`tighten` stage and everything since has reasoned from it. The stage figure is
correct; the inference from it was not. The tightening *algorithm* is 0.13 % of
routing. What the stage actually spends its time on is constructing — and
discarding — the free space, the triangulation and the via sites that tightening is
performed against.

**The literature's answer to M17c's question is unambiguous and it has a date on
it: 1997.** The canonical rubber-band router maintained an incremental
constrained-Delaunay mesh per layer, said so in one sentence, and gave the reason.
The modern algorithms for doing that are obtainable, one of them is deterministic
and comes with a proof, and this project's existing 1e-6 mm coordinate snapping puts
it on the integer lattice that makes such predicates safe. **But the profile says
two cheaper things come first**, and the honest recommendation is to take those
before deciding whether the third is still worth its risk.

**One gap closed, three opened.** Dayan's thesis is no longer a second-hand
citation; the toporouter note's markers on it can be resolved, C3's expected gain
re-priced from 7–16 % down to ~3.4 %, and C5's confidence lowered further. The new
gaps are Lu's 1991 dynamic-CDT thesis, NCTU-GR's two-stage cost function, and any
algorithmic account of TopoR whatsoever — all three named, none guessed at.

**And the standing rejection of learned routing is now an argument rather than an
assumption**, with the field's own evaluation protocol as the evidence, a named
exception that could survive the objection, and three conditions under which the
question reopens.

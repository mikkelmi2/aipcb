# Placement quality: the gains that are probably upstream of the router (M20)

**Why this exists, and why now.** Three milestones of router work have produced a
consistent shape of result:

* **M17a** measured the via opportunity M16's proxy had estimated at 37 of 90 layer
  changes. The geometry's answer was **two collapsible spans out of 55** — four
  vias, on one board. The proxy overstated the prize by an order of magnitude, and
  this router's vias are, with four exceptions, load bearing.
* **M17c** halved corpus routing time — 61.863 s to 29.476 s, **−52 %** — with ten
  of eleven board hashes unchanged. Runtime is no longer the binding constraint it
  was when the [toporouter postmortem](../notes/toporouter-postmortem.md) named it
  as the thing that actually kills topological routers.
* **M18** found the remaining routing time is geometry reconstruction, not
  algorithm, and [M19](m19-incremental-geometry.md) is spending it.

Put together: the router is getting fast, and the quality levers *inside* it keep
measuring smaller than they looked. **The remaining quality gains most likely live
upstream** — in a placement that hands the router a better problem. That is not a
new observation; the [roadmap has said placement is the next obvious
gap](../roadmap.md#placement) since M9, with the reason that *a router measured on
badly placed boards is being measured on the wrong thing*. What is new is that the
router-side candidates have been priced, and they are small.

**This milestone evaluates. It does not assume.** The candidates below go to the
same judge as every router candidate since M16: `aipcb bench` over the whole
corpus, against `bench/results/baseline.json`, before and after, in the delivery
report. A placement change with no numbers is not finished either.

## Sequencing

**After the `pcie-sata` fab round.** Placement moves copper on every board it
touches, including the flagship, and the
[part-2 rule's reason](../roadmap.md#part-2-the-quality-candidates-and-the-rule-they-are-held-to)
applies at full strength: the copper measured and the copper produced should be the
same copper while boards are in flight. The queue the owner set is
[M19s/M19a/M19b → BOM/CPL export → fab round → M19c and this
milestone](m19-incremental-geometry.md#sequencing-and-the-fab-round).

## What does not change

The input. The three-level intent model — [ADR
0008](../decisions/0008-mech-placement.md), mechanical fixes as law, relative intent
above them, packing below — is what a scoring pass optimises *inside*. **A placer
that moves a `fixed:` position to shorten a ratline has misunderstood which of the
two is a fact about the world.** Every candidate here operates strictly in the
packing layer, beneath declared intent.

## M20a — Rotation optimisation

**The cheapest lever available, and the one to measure first.** Component placement
today chooses a position; orientation is whatever the packer left. For each
*movable* component, evaluate its four orthogonal orientations against the
directions of its pins' nets — a pin whose net pulls east wants to be on the east
side of the part — and keep the orientation that minimises the score.

* **Runtime cost is near zero by construction**: four evaluations per movable
  component, on a pin-count-sized sum, once, before routing. It does not iterate
  and it does not search.
* **Scope it to what may rotate.** A component with a declared orientation, a
  mechanical fix, or a connector whose face is the specification does not
  participate. The set of movable-and-rotatable parts is smaller than the set of
  movable parts and the milestone states which is which.
* **Determinism**: four orientations in a fixed order, ties broken by the lowest
  index, like every other tie-break in this project.
* **Expected effect: fewer crossings and less wirelength.** That is a hypothesis
  with a mechanism behind it, not a measurement. **Measure it.**

**Budget: ≤ +2 % total `aipcb build` wall clock**, which is generous for what it
does. **What it must show: reduced ratline crossings and reduced routed copper on
at least the corpus's denser boards, at unchanged completion.** Every golden file
that contains a rotated part moves, so the report shows the copper it costs as well
as the copper it saves.

## M20b — Routability-estimated positioning

Fold a congestion prediction into the packing cost, so that the packer prefers
arrangements the router will find easier rather than merely arrangements that fit.

* The estimate must be **cheap and honest about being an estimate** — a
  bin-density or cut-crossing count over the ratline set, not a trial route. A
  placer that routes to score a placement has become a router.
* aipcb has an advantage most placers do not: its capacity is a **real number in
  millimetres** on a real cut, not an integer track count on a grid. The estimator
  should use the same cut model `check_capacity` does, so that what the placer
  predicts and what the router later reports are the same quantity.
* **Larger and riskier than M20a**, and gated on it: if rotation alone moves the
  numbers materially, the cost/benefit of this one changes.

**Budget: ≤ +20 % total `aipcb build` wall clock. What it must show:** completion
or copper improved on `examples/congestion` and on M19s's stress board — the two
boards where routability is actually scarce — without regressing the easy boards.

## M20c — The validation experiment

**This is an acceptance line in its own right, not a footnote.** After M20a and
M20b land:

> **Re-run `aipcb bench` over the whole corpus and measure which router-level
> symptoms improve with zero router changes.** Wirelength, via count, crossings,
> iterations to convergence, `headroom_mm`, and the diagnostic codes each board
> emits. The router's source is untouched between the two runs; only the placement
> that fed it differs.

This is the experiment that settles the thesis the milestone opens with. If
router-level quality metrics move materially on placement changes alone, the claim
that the remaining gains are upstream is **measured** rather than argued, and it
re-prices every surviving router candidate — including [C1
spreading](../roadmap.md#part-2-the-quality-candidates-and-the-rule-they-are-held-to)
and [C4 route-to-the-net](../roadmap.md#part-2-the-quality-candidates-and-the-rule-they-are-held-to),
whose value is partly a function of how bad the placement they inherit is. If they
do not move, that is a negative result of the first importance and it goes into the
[postmortem note](../notes/toporouter-postmortem.md) §C under the closure rule like
any other.

## The candidates this is evaluated against

M20 is not scheduled in a vacuum. The bench harness judges it against what remains
on the router side, so the report states the comparison plainly:

| | Expected gain | Runtime budget | Status |
|---|---|---|---|
| **M20a rotation** | unmeasured; mechanism is sound and the cost is near zero | +2 % build | this milestone |
| **M20b routability packing** | unmeasured | +20 % build | gated on M20a |
| **C1 spreading** | headroom, not copper — a spread route is *longer* by construction | +15 % route | roadmap candidate |
| **C4 route to the net** | shorter copper, better completion; largest and most invasive, needs an ADR | +20 % route | roadmap candidate |
| **C3 detour pass** | ~3.4 % copper (Dayan, primary) | +30 % route | **rejected at M19** |
| **C5 pairwise ordering** | expected ≈ 0; ten of eleven boards converge in one iteration | +10 % route | roadmap candidate, low confidence |

C3 is in the table because its rejection is the calibration: **3.4 % of wire length
for +30 % runtime is the trade that failed**, and any candidate here is measured
against a bar that number sets.

## Groundwork, if a survey turns out to be needed

Only if M20a's measurement suggests the mechanism is worth more study, and under
the same licence and study discipline as [M18](m18-literature-survey.md) —
implement from papers, never from source, and record what could not be obtained as
a gap rather than summarising it from a title:

* **The ePlace / RePlAce family** — analytical placement by electrostatics, the
  modern academic baseline, with open published measurements.
* **Force-directed methods** — older, simpler, and closer in spirit to what a
  cluster-and-pack placer would grow into.

Neither is scheduled. They are pointers so that a future survey starts from names
rather than from a search box.

## Guardrails

* Declared intent is law. Nothing here touches `fixed:`, and the ADR 0008 hierarchy
  is what the scoring optimises inside, never around.
* `aipcb bench` is the judge, corpus and per board, before and after.
* Deterministic: fixed orientation order, index tie-breaks, iteration bounds rather
  than wall-clock bounds.
* Every candidate carries its budget before it is built, and a candidate that misses
  it is rejected with its numbers written down.

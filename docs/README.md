# Documentation map

Two kinds of document live here, and the difference matters.

**User documentation** describes what aipcb does today. Read it to use the tool.

**Engineering history** — reports, decisions, milestone specs — describes how it
came to do that, in the order it happened. It is kept because the reasoning is
often more useful than the conclusion, and because a project written mostly by AI
agents should be auditable. It is *not* a description of current functionality,
and a milestone spec in particular is a statement of what was *asked for*, which
is not always what was built.

---

## Maturity map

Not everything documented here is at the same level, and a map that does not say so
is misleading. These are the tiers the [README](../README.md#maturity-at-a-glance)
carries, with the document that goes deepest on each.

| | | |
|---|---|---|
| Source format, compilation, `check`, schematics | **stable** — hardened across fifteen milestones and guarded by invariants | [`format.md`](format.md), [`guide.md`](guide.md) |
| Manual layout — preserve, `sync-placement`, `routing: manual` | **stable** | [`workflows.md`](workflows.md) |
| Part placement | **basic** — clusters shelf-packed into the outline, unoptimised on purpose | [`roadmap.md`](roadmap.md#placement) |
| Autorouting | **beta** — finishes the example corpus, has never met a board this project did not design | [`topology.md`](topology.md), [`routing-costs.md`](routing-costs.md) |
| SI simulation | **beta** — ten of eleven flagship links extract physically | [`reports/m13.md`](reports/m13.md) |
| Freerouting bridge | **new** — landed in M14e | [`external-routers.md`](external-routers.md) |

`aipcb route` says the beta part out loud, once per invocation, and the `--json`
report carries it as `routing.maturity` so an agent does not have to read prose.
What has to be true for each label to come off is in
[`roadmap.md`](roadmap.md#maturity-and-graduation) — conditions rather than dates.

A **beta** label here is information, not a warning: routing is one declared step,
it fails loudly rather than silently, and `routing: manual` or the bridge take it
out of the loop entirely. Everything in the stable rows works the same either way.

---

## User documentation

| | |
|---|---|
| [`guide.md`](guide.md) | **The long tour.** A single continuous read-through of every command and what it produces — this was the README until M15, kept whole. Start here if you would rather read than look things up. |
| [`format.md`](format.md) | **The source format.** The reference for every key in a design file — parts, nets, modules, board stack-up, net classes, pours, mechanical constraints, simulation. Start here after the README's quickstart. |
| [`workflows.md`](workflows.md) | **How the tool is used.** The three routing modes, the build/check loop, what survives a rebuild, how manual edits in KiCad are preserved, and what to do when a board can't be finished automatically. |
| [`external-routers.md`](external-routers.md) | **Handing a board to another router.** Exporting Specctra DSN, running Freerouting (or anything else), importing the SES back, and what aipcb checks about what came back. |
| [`topology.md`](topology.md) | How the topological router thinks: triangulation, the routing graph, and what "topological" buys over a grid. |
| [`routing-costs.md`](routing-costs.md) | The cost model the router negotiates with, and which knobs a design file can turn. |
| [`roadmap.md`](roadmap.md) | What is deliberately not built yet, and why. |

## Engineering history

| | |
|---|---|
| [`reports/`](reports/) | **Delivery reports — the best place to start.** One per milestone, each stating what was asked for, what was delivered, what was *not* delivered, and the measurements behind every claim. They are written to be read by someone who wasn't there. [`reports/m14.md`](reports/m14.md) is the most recent and the most representative. |
| [`decisions/`](decisions/) | **Architecture decision records.** Thirteen of them, each recording a choice, the alternatives measured, and the conditions under which the choice should be revisited. [ADR 0001](decisions/0001-kicad-io.md) (why we write KiCad files ourselves) and [ADR 0009](decisions/0009-pours.md) (what `pcbnew` can and cannot do headlessly) are the two most load-bearing. |
| [`milestones/`](milestones/) | **The specifications work was done against**, in execution order, each paired with its report. These are historical prompts. See the guard note at the top of [`milestones/README.md`](milestones/README.md) before reading one as documentation. |

## If you are new

Read the [README](../README.md), then [`format.md`](format.md), then run the
quickstart. If you want to know *why* the tool is shaped the way it is — and the
answer is usually more interesting than the shape — read
[`reports/m14.md`](reports/m14.md) and follow the ADR links from it.

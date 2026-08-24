# Milestones

The milestone specifications this project is built from, in execution order, each
paired with its delivery report in [`docs/reports/`](../reports/). A milestone file
is the *prompt* — what was asked for; the report is what was actually delivered,
with the numbers.

> **These files describe planned or historical work.** They are only relevant when a
> milestone is explicitly being executed. If you are doing ordinary development
> work, do not read a future milestone file as a description of existing
> functionality — nothing beyond the executed rows below is built. For what the project *does*
> today, read [`README.md`](../../README.md), [`docs/format.md`](../format.md) and
> the ADRs in [`docs/decisions/`](../decisions/).

| File | Milestones | Status |
|---|---|---|
| [`m1-m7-base.md`](m1-m7-base.md) | M1–M7 — source format, KiCad emission, check loop, query layer, incremental builds, topological routing | executed |
| [`m8-multilayer.md`](m8-multilayer.md) | M8 — layered triangulations, negotiated congestion, differential pairs | executed |
| [`m9-mech-placement.md`](m9-mech-placement.md) | M9 — board outline, mechanical placement, fanout, honest failure | executed |
| [`m10-pours.md`](m10-pours.md) | M10 — copper pours, stitching vias, plane integrity | **blocked** — see [`m10.md`](../reports/m10.md) |
| [`m11-highspeed.md`](m11-highspeed.md) | M11 — controlled impedance, edge connectors, via transitions | pending |
| [`m12-simulation.md`](m12-simulation.md) | M12 — SI simulation integration (openEMS/gerber2ems) | pending |
| [`orchestrator-m10-m12.md`](orchestrator-m10-m12.md) | runs M10 → M11 → M12 unattended, with gates between them | pending |
| M13 | routing correctness, the impedance model, the skew verdict | executed — [`m13.md`](../reports/m13.md) |
| M14 | readable schematics, optional routing, the external-router bridge | executed — [`m14.md`](../reports/m14.md) |
| [`m15-public.md`](m15-public.md) | M15 — hygiene sweep, licence package, the public README, contributor surface, CI | executed — [`m15.md`](../reports/m15.md) |
| M16 | toporouter lessons, part 1 — the capacity check's honesty, the exposure guards, the benchmark harness | executed — [`m16.md`](../reports/m16.md) |
| [`m17-measured-improvements.md`](m17-measured-improvements.md) | M17 — router improvements, part 2: via minimisation, retrace elimination, stretcher performance | executed — [`m17.md`](../reports/m17.md) |
| [`m18-literature-survey.md`](m18-literature-survey.md) | M18 — the routing literature survey (documentation only) | executed — [`m18.md`](../reports/m18.md) |
| [`m19-DRAFT.md`](m19-DRAFT.md) | M19 — router improvements, part 3: the field that gets rebuilt | **DRAFT, not approved** — written by M18 for the owner's review; nothing scheduled, nothing started |

The delivery report requirement was introduced with M10, so the earlier
milestones ran without one. [`m8.md`](../reports/m8.md) was reconstructed after the
fact from the repository's own history and says so at the top;
[`m9.md`](../reports/m9.md) was written from that milestone's own measurements
immediately after it landed. There is no report for M1–M7.

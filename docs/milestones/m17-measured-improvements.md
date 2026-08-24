# Router improvements, part 2: the measured candidates (M17)

The M16 baseline turned the part-2 plan into a ranked list with numbers attached. This milestone implements the three candidates the baseline proved, each against the harness with a runtime budget, and rejects-with-numbers anything that doesn't pay. No literature research here — that is M18; this milestone spends only evidence that already exists.

## Session context (fresh session)

Read `docs/reports/m16.md` (the baseline table is the reference), the part-2 roadmap entry, and `docs/notes/toporouter-postmortem.md` §C. The M16 baseline in `bench/results/` is the judge: every change lands with a `bench --compare` against it, and the acceptance bar is stated per candidate below.

## M17a — Via minimization (the documented feast)

Baseline: 37 of 90 layer changes were made where no corridor was half full — over a third of vias are potentially unnecessary.

- Post-routing pass: for each layer change, test whether the route's topology admits staying on-layer within capacity (the special-cuts-corrected capacity check from M16a is the arbiter — this is why soundness came first); collapse the via pair where it does
- Deterministic, order-stable (process nets in canonical order), idempotent (second run finds nothing)
- Budget: runtime ≤ +10 % on the baseline corpus. Target: measurable via-count reduction on the boards behind the 37/90 number; report per-board before/after
- Controlled-impedance nets: excluded unless their class's rules still hold after collapse (verify, don't assume — a via removal changes the geometry M11e audited)

## M17b — Retrace elimination (E2's case as the acceptance test)

Baseline handed this one a named defect: pcie-sata GND U1.17→U1.49 travels 4 mm, hops layers for 0.55 mm, hops back, and retraces its own path — ~8 mm of copper and two vias for nothing.

- Detect and eliminate self-retracing and trivial out-and-back excursions in tightened routes (the E2 warning from M16b already detects; this makes the fix)
- Acceptance: the E2 case on pcie-sata resolves (warning gone, copper and via count drop by the measured waste), no example regresses on any bench metric, budget ≤ +5 % runtime
- The E2 warning stays in the suite as the guard that the fix keeps working

## M17c — Stretcher performance (where 83–94 % of the time lives)

Baseline: tightening dominates routing time on every large board; negotiation never exceeds 0.50 s. All speed work belongs here. This candidate is *engineering*, not research — the algorithmic question (is the stretcher asymptotically optimal or constant-slow?) belongs to M18; here, harvest the constant factors:

- Profile the stretcher on the two slowest boards (bench has the stage timing; add finer-grained profiling temporarily, remove before commit per the M16 guardrail about not perturbing measurements)
- Expected harvest, subject to the profile: redundant geometry recomputation across tightening iterations, shapely object churn, unnecessary re-tightening of unchanged nets (incrementality gaps), obvious vectorization
- Budget: this one *reduces* runtime — target a measured ≥ 25 % stretcher-time reduction on the slowest boards without any quality metric regressing (bench proves both). If the profile shows the time is genuinely irreducible constant work, report that finding honestly — it sharpens M18's question
- No algorithm replacement in this milestone; if the profile screams for one, write it into the M18 brief instead

## M17d — Update the extrapolation

Re-run the scaling fit (connections × triangulation size, the R² 0.97 line) after a–c land. The 900-connection ≈ 31 min extrapolation is the capability-ladder number; publish the new one in the report and update wherever the old number is cited.

## Rejection rule

Any candidate that misses its budget or regresses quality is rejected with its numbers recorded in the postmortem note's §C — the closure rule from M16d. A rejected candidate is a result, not a failure.

## Roadmap: M18 — the literature survey (documentation only, write the entry)

Write the M18 brief into the roadmap: a study session in the toporouter-postmortem style covering (1) tightening theory — the funnel algorithm (Hershberger–Snoeyink, minimum-length paths of a given homotopy class), Chazelle's linear-time techniques, TopoR/Eremex's published papers on arc-based tightening, and another attempt at Dayan's full thesis; (2) the negotiated-congestion lineage — BoxRouter, NTHU-Route 2.0, FastRoute 4.x, NCTU-GR and the ISPD-contest refinements, mined for cost-function techniques (low priority: negotiation is already < 0.5 s — this is quality research, not speed); (3) modern/ML routing surveyed for completeness with the standing hypothesis that non-determinism disqualifies it from the core, rejection documented like the evolution candidate. Deliverable shape: docs/notes/routing-literature.md, same discipline as the toporouter note — technique → aipcb component → expected gain → runtime risk → candidate or rejected-with-reason. Include M17c's profile findings as input: the survey should answer whatever question the profiling left open.

## Acceptance

- M17a: via reduction measured per board, budget held, idempotence tested, controlled-impedance exclusion tested
- M17b: E2 resolved, guard retained, no regressions, budget held
- M17c: ≥ 25 % stretcher-time reduction on the slowest boards or the honest irreducibility finding; no quality regression either way
- M17d: new extrapolation published
- Every change: bench --compare against the M16 baseline committed alongside, deterministic, byte-stable goldens (or updated with the diff explained — via/retrace removal legitimately changes copper; the explanation is the acceptance)
- Suite green, mypy strict, committed and pushed

## Guardrails

- The M16 baseline is the judge; no candidate lands without its compare
- No literature-derived techniques in this milestone — evidence that exists today only; new ideas go to the M18 brief
- Determinism, license wall, and fail-safe culture as always
- When in doubt, stop and report

## Delivery report (required, in-repo)

`docs/reports/m17.md`: per-candidate numbers against budget, the per-board via/retrace tables, the stretcher profile findings, the new scaling fit, any rejections with their numbers, and the updated baseline (re-baseline after landing, clearly marked as the new reference). Measured claims over asserted ones. Commit and push.

# Upgrade: high-speed capability — controlled impedance, edge connectors & via transitions (M11)

Target: make aipcb capable of carrying a real high-speed board — the reference project is a PCIe x1 to 4-port SATA controller (e.g. ASM1064/JMB585 class): one PCIe Gen3 lane + refclk, eight SATA III pairs, all at 85/100 Ω controlled impedance, QFN/BGA controller, gold-finger edge connector. The goal is **not** to promise fully autonomous Gen3 routing — it is to (a) route controlled-impedance pairs with proper discipline, (b) generate the deterministic patterns such boards need (edge fingers, pair via transitions, AC coupling), (c) *verify and report* what the router cannot guarantee, and (d) hand over honestly where the bar isn't met. A human finishing 2 of 11 pairs manually is success; silently delivering 11 marginal pairs is failure.

Build order: M11a → M11e. M11d (stretcher environment control) is the research-heavy piece — everything else must not block on it.

## Before writing code

1. Choose and document the reference stackup for the new example (4-layer, sig/gnd/pwr/sig, standard 1.6 mm) and compute target trace geometries for 85 Ω and 100 Ω differential on it (microstrip outer layers) using standard formulas (e.g. IPC-2141-style approximations); record in `docs/decisions/000x-highspeed.md`. These numbers drive net-class defaults in the example
2. Read the M8c coupled-pair implementation and its refusal conditions; list which refusal causes M11 must eliminate and which remain legitimate
3. Read the M9e generator architecture — M11b/M11c are new generators under the same regime

## M11a — Controlled-impedance net classes

Extend net-class schema:

```yaml
net_classes:
  pcie_pair:
    class: diff_pair
    impedance_diff: 85       # ohm; with the stackup this derives width/gap
    max_skew: 0.125          # mm, intra-pair
    coupling: tight          # stretcher must keep the pair coupled continuously
    reference: In1.Cu        # required continuous reference plane
    max_uncoupled: 2.0       # mm total allowed uncoupled length (pad entries etc.)
    priority: 95
    rip_up: protected
```

- Width/gap derived from `impedance_diff` + stackup (the step-1 formulas), overridable explicitly; validation warns when explicit values disagree with the computed target by >10 %
- `reference:` declares which plane the pair depends on — consumed by the M11e checks, not by the router
- These are per-class defaults; the PCIe/SATA example sets them once and all 11 pairs inherit

## M11b — Edge-connector footprint integration

Card-edge fingers exist as ready-made footprints in KiCad's libraries (e.g. `Connector_PCBEdge`, PCIe x1/x4 variants) — do **not** build a finger generator. What's needed is correct integration of an edge footprint into the aipcb model:

```yaml
components:
  J_PCIE:
    part: PCIe_x1_edge          # maps to the KiCad library footprint in the parts DB
    role: edge_connector
placement:
  J_PCIE:
    fixed: {edge: south, offset: 10}   # edge footprints are always edge-fixed
```

- Add the chosen KiCad edge footprint(s) to the component database with full pin roles (per pad instance — edge connectors have many same-numbered/mirrored pads, exactly the M7 lesson)
- `role: edge_connector` triggers the integration behaviors:
  - The footprint must sit *on* the outline edge; validation errors if the fingers don't coincide with the board boundary within tolerance
  - The keying notch: verify the outline (M9-outline) has a cutout/notch where the footprint expects it — validate against the footprint's geometry, and emit a suggested `board:` cutout block in the error message if missing, so the fix is copy-paste
  - Pour keepout around the finger area (configurable clearance), fed to M10 zones automatically
  - Routing terminals at the finger inner ends; the finger area itself is a fixed obstacle
  - Board-thickness sanity check against the slot spec (warn), and a fab-documentation note for the chamfer/bevel (process step, not geometry aipcb produces)
- Net mapping happens the normal way — nets connect to the footprint's pads in the source like any component; no special pad-map syntax
- Everything here is validation + integration glue, not generation; the footprint is the footprint

## M11c — Pair via-transition & AC-coupling generators

Two more patterns from the M9 roadmap candidate list, now needed for real:

- **Pair via transition**: when a coupled pair changes layers, generate the transition as one validated pattern — paired signal vias at matched geometry, plus ground return vias adjacent (count and max distance configurable per net class, default 2 within 1.0 mm), with pad/antipad sized from the rules. The transition is a single topology event (M8c already models paired-via-columns); this generator gives it correct local geometry. Report per transition: return-via count achieved and stub length (through-via stub = remaining barrel below the exit layer, computed from stackup)
- **AC coupling**: series capacitors in high-speed pairs (PCIe TX requires them) are a routing discontinuity. Support a footprint-inline pattern: the pair routes to the cap pair's pads symmetrically, caps placed side-by-side perpendicular to the route, minimal uncoupled length. Source-side: mark the caps with `role: ac_coupling, for: <pair>` and the placer/router treat the pair-cap-pair as one routing object with the caps as an internal waypoint gate
- Both generators: output is ordinary tracks/vias/placements, counted against `max_uncoupled` budgets

## M11d — Stretcher environment discipline (bounded scope)

The structural weakness named earlier: tightening optimizes length, impedance wants constant environment. Do **not** attempt full environment-controlled tightening. Implement exactly these bounded rules for nets whose class has `impedance_diff` set:

1. **Standoff corridor**: tighten against `clearance × k` (default k=3 for controlled-impedance classes, configurable) instead of bare minimum — buys environment stability cheaply
2. **No wall-hugging**: if a tightened controlled-impedance segment runs parallel to another copper feature closer than `3 × gap` for longer than `5 × gap`, flag it (M11e report) and attempt one re-tighten with that feature's clearance inflated; if that fails, the flag stands
3. **Coupling continuity**: enforce `max_uncoupled` as a hard budget; exceeding it is a refusal (existing fail-safe path), with the uncoupled segments listed
- If these rules make a pair untightenable, hand over via M9f — that is the correct outcome, not a bug to engineer around

## M11e — High-speed verification report

The honest substitute for SI simulation — rule-based checks with structured output, same spirit as the plane-integrity report:

- **Reference continuity**: for each controlled-impedance net, project its path onto its declared `reference` plane (post-fill) and report every crossing of a plane split, void, or clearance gap, with location and length — the classic return-path check
- **Geometry audit**: actual width/gap along each pair vs. the impedance-derived target; report deviations and where
- **Skew and length**: intra-pair skew after meanders, inter-pair where a `matched_group` is declared
- **Via stubs**: per transition, stub length vs. a configurable warn threshold (default 0.5 mm)
- **Coupling audit**: total uncoupled length vs. budget, per pair
- Output: human text + `aipcb check --json`, every finding pointing at source lines; severity is *warning* by default (these are engineering-judgment items), with per-class option to promote to error
- Explicitly documented in the report header and README: this is rule-based verification, not electromagnetic simulation; Gen3 sign-off still warrants human review or external SI tools

## Acceptance

- **The reference example**: a `pcie-sata` example board — PCIe x1 edge connector (M11b), controller QFN with fanout, 4 SATA connectors, all 11 pairs declared with proper classes, AC coupling caps on the TX pairs, 4-layer stackup from step 1. Requirements: build/check/export pipeline completes; ≥ the SATA pairs route and tighten within all M11d rules with 0 DRC violations; any pair that cannot meet the rules is handed over via M9f with reasons, never delivered marginal; the M11e report runs clean (no reference-plane crossings, stubs under threshold) on everything that routed
- Edge connector integration: the library footprint sits on the outline edge, the notch-consistency validation passes (and a test proves it *fails* helpfully with the suggested cutout block when the notch is missing), pour keepout present around fingers, board opens correctly in KiCad
- Pair via transition unit test: return vias placed within spec, stub length computed correctly against the stackup
- AC coupling test: pair-cap-pair routes as one object, uncoupled length within budget, symmetric entry verified geometrically
- Wall-hugging test: a deliberately crowded pair triggers rule 2, the re-tighten resolves it or the flag appears in the report
- A geometry-audit test where an explicit (deliberately wrong) width override produces the >10 % validation warning and the M11e deviation finding
- All prior examples unaffected: byte-stable rebuilds, 0 DRC

## Out of scope (record in `docs/roadmap.md`)

- Electromagnetic/SI simulation and eye-diagram prediction — M11e is rule-based by design; note external-tool integration (e.g. exporting geometry for openEMS) as a future candidate
- Full environment-controlled tightening beyond the three M11d rules
- Back-drilling, blind/buried-via optimization for stub elimination

## Guardrails

- M11d is the only permitted stretcher change, limited to the three rules as specified; anything further, stop and ask
- Generators (M11b/M11c) follow the established regime: deterministic, UUID-mapped, per-pad-instance keyed, preserve-compatible
- The fail-safe culture is the point of this milestone: every place where high-speed correctness cannot be guaranteed must surface in a report or a hand-over — never in silence
- Schema backward compatible: designs without the new blocks rebuild byte-identically

## Session context (this runs in a fresh session)

Before anything else, read `docs/reports/m10.md` and any earlier reports in `docs/reports/`, plus the ADRs in `docs/decisions/` relevant to zones, generators, and the stretcher. The M11e reference-plane checks build directly on M10's filled zones — the fill mechanics and determinism findings in the M10 report are prerequisites, not background. If an expected report is missing (reports before M10 predate the report requirement), reconstruct what's needed from `docs/decisions/`, the git log, and the code itself; note the gap in your own report rather than stopping.

## Delivery report (required, in-repo)

When the milestone is complete, write the delivery report to `docs/reports/m11.md` — in the repo, not only in chat. Same style as the earlier reports: what was built and verified (with numbers), which pairs on the pcie-sata example routed within the M11d rules and which were handed over (with the stated reasons), empirical findings, decisions and why, defects found and how they surfaced, anything deliberately not built, and open questions for whatever comes next. Measured claims over asserted ones.

Include the performance table (same format as the M10 report): wall-clock time for the full pipeline on every example, broken down by stage, aipcb stages separated from kicad-cli calls. Compare against the M10 baseline and flag any stage that regressed; report the pcie-sata board's numbers prominently, including how long the M11d re-tightening rounds and the M11e analysis add.

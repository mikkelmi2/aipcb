# Upgrade: copper pours, stitching vias & plane integrity (M10)

Boards today ship without copper pours — nearly every real board wants ground pour on outer layers and often split power planes. Add pours as first-class source intent, let KiCad's own zone-fill engine do the filling, generate stitching vias as a deterministic pattern, and report plane integrity as structured feedback. No routing-algorithm changes.

## Before writing code

1. Verify empirically what `kicad-cli` offers for zone refill in the installed KiCad version (e.g. `pcb drc` behavior on unfilled zones, any explicit refill/fill command or flag) — the plan below assumes zones can be filled headlessly before DRC; adapt if the CLI surface differs and record findings in `docs/decisions/000x-pours.md`
2. Test whether KiCad's zone fill is deterministic across runs on the same input; the stability policy below depends on the answer — measure, don't assume
3. Read the M9e fanout generator — stitching vias (M10c) reuse the same pattern-generator architecture

## M10a — Pours as source intent

Extend the schema (and `docs/format.md`):

```yaml
pours:
  - net: GND
    layers: [F.Cu, B.Cu]
    scope: board                 # whole board minus keepouts/cutouts
    priority: 0
  - net: VDD_3V3
    layer: In2.Cu
    region: {rect: [[10, 10], [60, 40]]}   # split plane
    priority: 1                  # higher priority wins where zones overlap
    connect: thermal             # thermal | solid (default thermal)
    clearance: 0.3
    min_width: 0.25
    remove_islands: always       # always | never | below_area
```

- Emit as KiCad zone objects with matching parameters; do **not** reimplement zone filling — KiCad's fill engine is the reference DRC checks against
- Per-pad-role override for connection style: pads with `role: thermal_pad` (or an explicit list) get solid connection while the zone default stays thermal. Keyed per pad instance, as always
- Zones respect M9 outline, cutouts, and any keepout intents; `scope: board` derives its boundary from the outline polygon
- Hatched fill: expose KiCad's hatch parameters as an optional passthrough block only if it costs nothing extra; do not build UI/validation around it
- Validation: pour net must exist, layers must be copper layers in the stackup, region inside outline, overlapping same-layer zones must have distinct priorities

## M10b — Fill integration & the stability policy

- `aipcb build` emits zones **unfilled** — build stays fast and byte-stable as before
- `aipcb check` fills zones (via the CLI surface found in step 1) before running DRC, since DRC requires filled zones; fill happens in a temp copy or is clearly staged so build output remains the unfilled reference
- **Stability policy (state this explicitly in `docs/format.md` and README)**: the byte-identical guarantee applies to *build output* (source → unfilled zones). Fill geometry is a derived artifact, regenerated at check/export time. If step 2 showed KiCad's fill is deterministic, filled output should also be stable in practice — but the *guarantee* covers unfilled build output only
- `aipcb export` fills before generating Gerbers
- All five-plus existing examples gain a GND pour and must remain 0 DRC violations after fill

## M10c — Stitching vias (pattern generator)

Reuse the M9e generator architecture — deterministic pattern generation, UUID-mapped, never a router:

```yaml
stitching:
  - net: GND
    between: [F.Cu, B.Cu]
    pattern: grid               # grid | edge | ring
    pitch: 5.0
    via: {drill: 0.3, diameter: 0.6}
  - net: GND
    pattern: edge               # along board outline
    pitch: 3.0
    inset: 1.0
```

- `grid`: regular lattice over the intersection of the zones' areas; skip positions that would violate clearance to tracks, pads, other vias, cutouts, or the outline (per-instance obstacle keying)
- `edge`: row following the outline polygon (including arcs) at `inset` distance
- `ring`: circle of vias around a named component or region (for shielding a noise source) — take a `around:` reference
- Skipped positions are silent; total placed/skipped counts go in the build report
- Stitching vias are ordinary vias in the output: obstacles to any later routing run, preserved semantics like everything else
- Runs after routing and before fill in the pipeline ordering

## M10d — Plane-integrity report

After fill, analyze each pour and report structured facts — this is feedback for the agent loop, not a pass/fail gate:

- Per zone: number of disconnected islands, area of largest island as fraction of zone scope, list of island bounding boxes
- Flag zones where `remove_islands` deleted copper (the agent may want to know the plane is thinner than intended)
- Optional threshold in source (`min_contiguous: 0.7`) that turns fragmentation into a check *warning* — never an error, since fragmented-but-functional is common
- Output in both human text and `aipcb check --json`, pointing at the pour's source lines
- Implementation: read the filled zone polygons back from the checked board and do connectivity analysis with shapely; keep it pure-functional

## Acceptance

- All existing examples grow a `pours:` block (GND on outer layers minimum) and pass: build byte-stable (unfilled), check 0 DRC violations (filled), export produces Gerbers with filled copper
- A split-plane example: 4-layer board where In2.Cu carries two pours (GND and VDD region) with priorities — filled, DRC-clean, and the integrity report shows both planes contiguous
- Thermal-relief test: a zone-connected pad with `connect: thermal` shows spokes in the filled output; a `thermal_pad`-role pad connects solid — verified by reading back the filled board, not by trusting parameters
- Stitching: grid + edge patterns on an example, deterministic positions across runs, skipped-position counts reported, 0 DRC violations with stitching present
- Integrity report: a deliberately fragmenting example (a track slicing a pour) reports the correct island count and largest-island fraction; with `min_contiguous` set, check emits a warning pointing at the pour's source line
- Determinism measurement from "Before writing code" step 2 recorded in the ADR, and the stability policy documented user-visibly

## Out of scope (record in `docs/roadmap.md`)

- Pour-aware routing (return-path preservation, fragmentation-avoiding costs) — research; the integrity report is the M10 answer
- Current-capacity analysis of pours as conductors
- Reimplementing zone fill in aipcb — deliberately rejected (KiCad's fill is the reference); record as ADR

## Guardrails

- Router and stretcher algorithms untouched; stitching is a pre-fill pattern stage and pours are emission + analysis only
- Same bars as always: deterministic build, UUID-mapped elements, per-pad-instance keying, backward-compatible schema (existing designs without `pours:`/`stitching:` blocks rebuild byte-identically)
- If KiCad's CLI cannot fill zones headlessly in the installed version, stop and present options before working around it

## Session context (this runs in a fresh session)

Before anything else, read the reports in `docs/reports/` and the ADRs in `docs/decisions/` — they are the only memory carried between milestones. If an expected report is missing (reports before M10 predate the report requirement), reconstruct what's needed from `docs/decisions/`, the git log, and the code itself; note the gap in your own report rather than stopping.

## Delivery report (required, in-repo)

When the milestone is complete, write the delivery report to `docs/reports/m10.md` — in the repo, not only in chat, since the next milestone runs in a fresh session and this file is its starting context. Same style as the earlier reports in `docs/reports/`: what was built and verified (with numbers), the empirical findings (the kicad-cli fill surface and the fill-determinism measurement from "Before writing code"), decisions made and why, defects found and how they surfaced, anything deliberately not built, and open questions the next milestone (M11 — high-speed: reference-plane checks build directly on the filled zones from this milestone) should know about. Follow the established pattern: measured claims over asserted ones.

Include a performance table: wall-clock time for the full pipeline (`validate → build → route → check → export`) on every example board, broken down by stage — aipcb's own stages (route, stitching, analysis) separated from kicad-cli calls (fill, DRC, gerber export) — so it's visible where time actually goes. This becomes the baseline future milestones measure regressions against.

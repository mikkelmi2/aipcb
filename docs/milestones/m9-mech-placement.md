> **STATUS: executed.** Kept as the historical specification; see `docs/reports/` for the delivery.

# Upgrade: board outline, mechanical placement, fanout & honest failure (M9)

The placement model today is purely relative (groups, proximity, keep-apart), and the board outline exists only as a loosely specified intent. Real boards have a precise mechanical boundary — often non-rectangular, with cutouts and slots — and components whose positions are dictated from outside: connectors aligned to enclosure openings, mounting holes, buttons, LEDs meeting light pipes, antennas. These are hard boundary conditions, not preferences the auto-placer may negotiate. In addition, dense-pitch packages need pattern-based fanout before general routing can succeed, and the router needs an honest hand-over path for what exceeds its capability. This milestone makes the outline a first-class source object, builds a three-level placement model on top of it, adds a deterministic fanout generator, and gives the router a give-up mechanism — and makes the whole chain respect all of it.

Order matters: do M9-outline first — everything else in M9 places and validates *against* it. M9e/M9f come last.

### Before writing code

1. Read the current placement-intent schema, the outline handling in M3, and the M3 auto-placer; write `docs/decisions/000x-mech-placement.md` mapping the plan below onto them
2. List any places where placement code assumes every component is movable, and any places where the outline is assumed rectangular or treated as a bounding box
3. Check how the current examples define their outline and plan the migration (backward compat requirement below still holds)

### M9-outline — Board boundary as first-class source object

Extend the schema (and `docs/format.md`) with a `board:` block:

```yaml
board:
  origin: bottom_left        # explicit convention; source is Y-up
  outline:
    rect: [80, 50]           # shorthand for the common case
    corner_radius: 2
    # or full form:
    # polygon:
    #   - [0, 0]
    #   - [80, 0]
    #   - {arc_to: [85, 5], center: [80, 5]}
    #   - [85, 50]
    #   - [0, 50]
  cutouts:
    - rect: [[70, 20], [78, 30]]
      reason: "flex cable to display"
    - slot: {from: [10, 48], to: [25, 48], width: 2}
  edge_clearance: 0.3        # feeds both KiCad's edge-clearance rule and the router
```

- **Coordinate convention**: source is Y-up with the declared origin; the emitter converts deterministically to KiCad's Y-down. Document the conversion in `docs/format.md` and add an explicit sign-convention test — this is exactly the class of bug the M7 postmortem flagged (portal orientation signs), so test it, don't trust it
- `reason:` on cutouts, same rationale as fixed placements: an AI agent must know the hole is mechanical law, not reclaimable routing area
- Emit outline and cutouts as `Edge.Cuts` geometry (lines and arcs); `edge_clearance` maps to the corresponding KiCad design rule so `kicad-cli pcb drc` enforces it natively
- Validation (in `aipcb validate`, cheap): polygon closes and is non-self-intersecting, arcs are geometrically consistent, cutouts lie strictly inside the outline and don't intersect each other
- **Propagation — this is the real work, not the Edge.Cuts export**:
  - *Placer*: pack components inside the actual polygon (not its bounding box — on an L-shaped board the difference is the entire missing corner); cutouts are forbidden zones for footprints and courtyards
  - *Router*: outline and cutouts become obstacles with `edge_clearance` in every layer's triangulation. Note that cutouts pierce all layers and can separate points homotopically — the topology model already treats obstacles correctly, it just needs to be fed these
  - The outline lives in its own stable block; changing it is intentionally a big diff
- Keep `rect` as the shorthand so existing examples migrate with one small block

### M9a — Three-level placement model in the source format

Extend the schema (and `docs/format.md`) with:

```yaml
placement:
  J1:                                # fully fixed: mechanical law
    fixed: {x: 0, y: 15, rot: 90, side: front}
    reason: "enclosure port opening, see mech/enclosure-v3.step"
  H1:
    fixed: {x: 3.5, y: 3.5}
    role: mounting_hole
  SW1:                               # partially constrained
    edge: {side: north, offset_range: [20, 40], rot: 0}
  D5:
    region: {rect: [[10, 10], [30, 25]]}   # anywhere inside this area
  # everything else: existing relative intents (groups, proximity, keep-apart)
```

- Coordinates are in the board reference frame defined by `board.origin` (M9-outline); `edge:` sides refer to the outline's bounding directions, and for non-rectangular boards an edge constraint clamps to the nearest actual outline segment on that side
- `reason:` is free text but strongly encouraged for `fixed` — it tells an AI agent *why* the part cannot move (and points to the mechanical file). Emit a lint warning when a `fixed` placement has no reason
- `side: back` on a `fixed` placement: keep the existing behavior (validate, warn, place on front) unless back-side mirroring has been implemented by now — check, don't assume
- Semantics: `fixed` > `edge`/`region` > relative intents. A relative intent involving a fixed part constrains only the *other* parts

### M9b — Auto-placer: fixed parts as anchors

- Place `fixed` components first, immovably; they become anchors in the force-directed/packing pass
- `edge`/`region` parts are placed by the optimizer but clamped to their allowed set (project back into the region each iteration)
- Relative intents pull movable parts toward their anchors as before — a USB PHY group should naturally gravitate to its fixed connector with no new mechanism
- Determinism bar unchanged: same source → identical placement

### M9c — Early conflict validation

Extend `aipcb validate` (cheap, no build needed) to catch:

- Two fixed footprints whose courtyards overlap
- A fixed part outside (or partially outside) the actual outline polygon — not its bounding box
- A fixed or placed part whose courtyard intersects a cutout
- An `edge`/`region` whose allowed set is empty, lies outside the outline, or is entirely swallowed by a cutout
- Infeasible relative intents given the fixed anchors: e.g. `max_distance: 2mm` between parts of two different fixed/edge groups that can never be closer than 10 mm. Exact infeasibility is hard; implement a conservative bound check (interval/bounding-box reasoning) and report as warning, not error, when uncertain
- Every diagnostic points to the source lines involved, as usual (outline/cutout diagnostics point at the `board:` block)

### M9d — Round-trip with manual adjustment

- If the user nudges a `fixed` part in KiCad, `aipcb build` currently would snap it back. Instead: detect the drift and report it — "J1 moved 0.4 mm from its fixed position in source" — with a new command `aipcb sync-placement` that writes the KiCad position *back into the YAML* for fixed/edge parts the user confirms. Source stays the single truth; the tool helps update it rather than silently overriding or being overridden
- Movable parts keep the existing M6 preserve behavior unchanged

### M9e — Fanout generator (pattern-based escape routing)

Dense-pitch packages (QFN, BGA) are where rubber-band routing hits its density ceiling: escape routing is not a general routing problem but a known geometric *pattern*. Solve it with a dedicated, deterministic generator that the topology layer orchestrates — never by bolting on a foreign autorouter:

- New source-level intent:

```yaml
fanout:
  U1:
    style: auto          # auto | dogbone | via_in_pad | none
    escape_layers: [In1.Cu, B.Cu]
    via: {drill: 0.2, diameter: 0.45}   # defaults from stackup/rules if omitted
```

- The generator produces the classic patterns: dog-bone vias for BGA (quadrant-based escape direction), direct short stubs for QFN perimeter pads, via-in-pad only when explicitly requested (flag it as a fab-cost warning)
- Output is ordinary tracks + vias, emitted deterministically, mapped to source like everything else. The escape endpoints (where signals have escaped the package area) become the *routing terminals* the rubber-band router sees — the fanout region itself is a fixed obstacle to the stretcher
- Escape must respect the package's own geometry: pad instances (never pad numbers — the M7 lesson), courtyard, and the outline/cutouts from M9-outline
- Unused pads get no fanout; power/ground pads may fan to multiple vias per current rules (net-class `min_width` implies via count — document the model)
- `aipcb route all` runs fanout generators first, then routes between escape terminals
- This is deterministic pattern generation under the same UUID/preserve/diff regime — explicitly not a second autorouter

### M9f — Honest failure: the give-up mechanism

Extend the fail-safe culture (coupled pairs already refuse with a reason) to routing capacity in general:

- When negotiated congestion cannot converge for a net, or a topology cannot be tightened DRC-clean after the iteration budget, the router must **hand over rather than deliver marginal geometry**: mark the net as `unrouted: over_complexity` in the check report, with the location/edge that blocked it and which nets own the contested capacity
- `aipcb check --json` lists handed-over nets so an AI agent can react (change placement, raise layer count, adjust priorities) and a human knows exactly what to route manually in KiCad
- Manually routed handed-over nets are then fixed obstacles via M6 preserve, and subsequent `route` runs treat them as law
- Never silently deliver a board with DRC violations as "routed" — the acceptance bar stays 0 violations on whatever *is* routed, plus an explicit, machine-readable list of what is not

### Acceptance

- New example board (or extend usb-port): enclosure-style scenario with a **non-rectangular outline** (at least one arc and one cutout with a `reason:`), a fixed USB connector at an edge, two fixed mounting holes, an edge-constrained button, and a region-constrained LED — builds, places (all courtyards inside the polygon, none over the cutout), routes completely with tracks respecting `edge_clearance` around both outline and cutout, 0 DRC violations against real KiCad, byte-stable
- Outline opens correctly in KiCad: `Edge.Cuts` contains the lines and arcs, and KiCad's own edge-clearance DRC is active with the source's `edge_clearance` value
- Sign-convention test: a deliberately asymmetric outline + fixed part round-trips source → KiCad with correct Y orientation (regression guard for the Y-down class of bugs)
- Existing examples migrate to a `board:` rect block and rebuild with unchanged placement and routing
- Validation tests for each conflict class in M9c, including at least one conservative-bound warning case and one part-over-cutout case
- A test proving relative intents cannot move a fixed part (the group deforms around the anchor instead)
- A routing test where a cutout separates two sensible paths and the topology correctly distinguishes going around it on either side
- `sync-placement` round-trip test: move in KiCad → sync → rebuild → byte-identical to the synced state
- **Fanout**: a new example with a QFN-32 (or similar dense package) where perimeter escape + rubber-band routing between escape terminals routes the board completely, 0 DRC violations, byte-stable; a BGA dog-bone unit test verifying quadrant escape directions and per-pad-instance obstacle keying
- **Give-up**: a deliberately over-constrained example where the router hands over at least one net with a correct machine-readable report, everything routed is DRC-clean, and a manually routed version of the handed-over net survives rebuild via preserve

### Out of scope (document as future work in `docs/roadmap.md`)

- MCAD import (`aipcb import-mech` reading DXF/STEP reference points into `fixed` blocks) — design the YAML so this becomes a pure generator later, but do not build it
- 3D clearance/height checking against an enclosure model
- Back-side placement mirroring, unless it already landed in M8
- Integration with external autorouters (Freerouting etc.) — deliberately rejected, not deferred: foreign routers break determinism, source mapping, and preserve semantics. Record this as an ADR so it isn't relitigated
- Further pattern generators (diff-pair via transitions with return vias, crystal routing, antenna feeds) — the fanout generator establishes the pattern-generator architecture; list these as candidates

### Guardrails

- Router and stretcher *algorithms* stay untouched. Permitted routing-side changes are exactly: (1) feeding outline and cutouts into the existing obstacle model with `edge_clearance`, (2) the fanout generator as a *pre-routing* pattern stage whose output is fixed obstacles + terminals, (3) the give-up reporting path. If anything beyond these is needed, stop and ask
- The fanout generator must satisfy the same bars as everything else: deterministic, UUID-mapped, byte-stable, per-pad-instance keyed
- Panelization (mouse bites, V-cut) is fab-level and out of scope; note it in `docs/roadmap.md`
- Schema changes must be backward compatible: all existing example designs validate and rebuild byte-identically after the mechanical one-line migration to a `board:` rect block, with zero placement or fanout blocks changed

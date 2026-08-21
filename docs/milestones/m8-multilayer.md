> **STATUS: executed.** Kept as the historical specification; see `docs/reports/` for the delivery.

# Follow-up prompt for Claude Code — run after M1–M7 are complete

Copy everything below the line into Claude Code once the base aipcb project builds, checks, and routes the example boards.

---

## Upgrade: true multilayer routing (M8)

### Current state (read this first — it reflects the actual M1–M7 delivery)

- M1–M6 complete; M7 complete **except**: meander length-matching and pair crossovers were not built (documented as such). Fold both into M8c below rather than treating them as separate work
- Via hops are modelled and validated in the topology layer, but **the stretcher rejects them** — multilayer escape is the missing piece, and it is exactly what M8a/M8d deliver
- **led-blinker and usb-port genuinely cannot be routed on a single layer** (0.65 mm-pitch micro-B fanout vs 0.25/0.2 mm rules — a physical constraint, not a router bug). These two boards are your *primary* multilayer acceptance tests, alongside the new 4-layer example below. When M8 is done, both must route completely
- Coupled pair routing currently self-checks its geometry and refuses to couple when it can't do so safely (with a stated reason). Keep that fail-safe behavior — M8c should make the refusals unnecessary, not remove the check
- `side: back` placement is validated but not implemented (warns, places on front). It is **not** in M8 scope; leave it as is unless mirroring falls out naturally from the layer work
- Hard-won lessons from M7 that apply directly here: obstacles must be keyed per pad *instance*, never per pad number (shield tabs share numbers); waypoints only constrain topology as gates straddling an obstacle, not as free points; portal orientation signs matter in Y-down coordinates; UUIDs must never land on structural tokens. Re-read the M7 postmortem notes/ADRs before designing

The current router treats layers as an afterthought: routing happens mostly on one layer, and via hops exist in the model but not in the stretcher. Upgrade it to genuine multilayer routing where layer choice, via placement, and congestion are optimized together. This is an evolution of the existing M7 machinery, not a rewrite — reuse the topology model, the stretcher, and the DRC loop.

### Before writing code

1. Read the existing `docs/topology.md` and the M7 ADRs; write `docs/decisions/000x-multilayer.md` describing how the plan below maps onto the current data structures, and what must change
2. Read up on: PathFinder negotiated-congestion routing (McMurchie & Ebeling), SURF rubber-band sketch routing, and Maley's homotopic routing — the design below assumes these ideas
3. Identify any M7a data-model assumptions that are single-layer-only and list them in the ADR before touching them

### M8a — Layered representation with via columns

- Maintain one constrained Delaunay triangulation **per signal layer**, over all obstacles on that layer (pads, board edge, keepouts, existing preserved tracks)
- Model a via as a **column**: a node that exists as an obstacle on *every* layer it physically passes through (respect the stackup's via types: through/blind/buried if defined), and that connects the triangulations of the layers it joins
- A route topology becomes: sequence of triangulation edges crossed, with via-column events marking layer transitions
- Realizability stays local: each triangulation edge has a capacity (edge length minus clearances of everything crossing it); a set of topologies is valid iff no edge is over-subscribed. Extend `aipcb route check` to verify this across all layers
- Stackup roles become routing law: layers marked as plane (ground/power) in the stackup are excluded from signal routing (infinite cost), unless a net class explicitly opts in

### M8b — Multilayer auto-topology with negotiated congestion

Replace the single-layer path derivation in M7c with shortest-path search in **one combined graph across all layers**:

- Nodes: triangulation edges on each signal layer + candidate via sites
- Candidate via sites: generated discretely — in free regions of the triangulation or on a coarse grid (~2× via pitch). Do **not** optimize continuous via coordinates in the search; the stretcher refines the exact via position later (a via is just a node both layers' rubber bands pull on)
- Edge cost function (make each term a named, documented, configurable parameter):

```
cost = length
     + via_cost × n_vias              # default equivalent to 3–10 mm of track; tune
     + layer_penalty(layer, netclass)  # ∞ for plane layers; low for preferred layers
     + congestion(edge)                # → ∞ as edge usage approaches capacity
     + direction_penalty               # soft H-layer/V-layer preference from stackup
```

- Routing loop = **negotiated congestion (PathFinder-style)**: route all nets, increase cost on over-subscribed edges, rip up and re-route the worst offenders, iterate until no edge is over capacity or an iteration limit is hit. Ripping up is cheap here — it removes a symbolic path, not geometry. Log convergence per iteration
- **Signal priority classes**: extend the source-format net-class schema with two fields (add them to the format layer and `docs/format.md` as part of this milestone):

```yaml
net_classes:
  clk_sys:
    priority: 90        # 0–100, default 50
    rip_up: protected   # never | protected | normal (default: normal)
```

  Priority drives the router in two places:
  1. **Initial ordering**: nets are routed in descending priority; within equal priority, fall back to the heuristic below
  2. **Rip-up cost**: in the negotiation loop, ripping up a net costs `f(priority)` — `protected` nets get a large multiplier (low-priority traffic detours around them), `never` nets are only ripped as a last resort before declaring failure, and the failure report must name which `never` net blocked convergence

  Defaults when priority is unset: diff pairs and matched groups behave as priority ~80, power ~60, everything else 50 — i.e. the heuristic ordering below is just the default priority assignment, expressed through the same mechanism, not a separate code path
- Net ordering (within equal priority): descending difficulty (length × congestion). Ordering sensitivity should shrink over iterations — add a test that shuffled input order converges to a valid (not necessarily identical) result, and that the *final* output is still deterministic for a given source (fixed iteration schedule, stable tie-breaking). Add a priority test: a low-priority net that would take the short path alone must detour when a `protected` high-priority net owns that corridor

### M8c — Cross-layer constraints (includes the two M7 leftovers)

- **Differential pairs as one object**: a pair is a single entity in the graph (one path, double capacity consumption, a via transition is one paired-via-column event). Never route the two sides independently and merge afterwards. The stretcher tightens them coupled, with gap from the impedance spec
- **Meander length-matching (M7 leftover)**: implement length matching / max skew by meander insertion in the slack regions the topology allows. The length model must account for via barrel length (from stackup thickness). Meanders must stay within the net's own corridor (no new capacity violations) and the result must remain DRC-clean
- **Pair crossovers (M7 leftover)**: support polarity swaps in differential pairs. Prefer the natural mechanism: a crossover at a paired via transition (the pair swaps sides across the layer change). If a same-layer crossover is ever needed, treat it as a small validated pattern, not free-form routing
- The existing coupled-pair self-check and refuse-with-reason behavior stays; success for this milestone means usb-port's two pairs now couple cleanly instead of refusing
- Layer preference per net class (`layer_pref`) feeds `layer_penalty`; add `layer_forbid` as well

### M8d — Stretcher integration & acceptance

- The stretcher must accept via-column hops: it runs per layer as before, but via columns are shared pull-points between the layers they join; tightening moves the via within its region to the equilibrium position
- Preserved manual tracks (from the M6 preserve mechanism) act as fixed obstacles in their layer's triangulation
- Acceptance, same bar as always (0 DRC violations via `kicad-cli pcb drc` against real KiCad, byte-stable rebuilds), on:
  - **led-blinker and usb-port route completely** — these currently fail single-layer for genuine physical reasons and are the primary proof that multilayer escape works
  - ldo-supply, routing-demo and diff-pair keep routing completely (no regressions)
  - A new **4-layer example board** (sig/gnd/pwr/sig stackup) in `examples/`: an MCU + USB diff pair + a small bus that cannot be routed on one layer
  - Plane layers contain no signal tracks
  - A congestion stress test: a deliberately tight example where single-layer routing must fail and negotiated congestion must converge
  - usb-port's diff pairs couple (see M8c) and, if a matching spec is set, meet skew after meander insertion

### Explicitly out of scope

- ILP/SAT global optimization (doesn't scale), machine-learned routers (non-deterministic), continuous via-position optimization in the search, autorouting of copper pours

### Guardrails

- Keep the stretcher and the graph search pure-functional and deterministic; all randomness (if any) seeded from source hash
- Every cost parameter documented in `docs/routing-costs.md` with its default and rationale
- If the existing M7 data model fights this design in some place, propose the migration in the ADR and ask me before breaking the source-format schema

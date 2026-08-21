> **STATUS: executed.** Kept as the historical specification; see `docs/reports/` for the delivery.

# Prompt for Claude Code

Copy everything below the line into Claude Code as your opening prompt.

---

## Project: "aipcb" — an AI-native schematic & PCB source format compiled to KiCad

### Vision

Build an open-source toolchain where the *source of truth* for an electronics design is a semantic, git-friendly, AI-readable text format (intent, constraints, topology), and KiCad is used as the *backend* (rendering, DRC/ERC, interactive editing, Gerber export). Think of it as "source code → compiler → binary", where KiCad files are the binary.

The primary consumer of this format is an AI agent iterating in a loop: edit source → compile to KiCad → run checks via `kicad-cli` → read structured feedback → fix. A human can open the generated `.kicad_sch` / `.kicad_pcb` at any time.

### Architecture (layers)

1. **Semantic schematic layer** (`design.yaml` or a small DSL — you propose, justify the choice)
   - Netlist-first: components with `part`, `role` (e.g. `decoupling`, `pull_up`, `snubber`), and `for:` back-references
   - Nets with classes (`power`, `diff_pair`, `analog`, ...), electrical attributes (voltage, max current, impedance)
   - Hierarchical, parameterized modules (e.g. `buck_converter(vin, vout, iout_max)`) instantiated like functions
   - Design intent & constraints as first-class fields: `keep_apart`, `max_distance`, `reason:` strings
2. **Layout-intent layer**
   - Placement intents (groups, proximity rules, keep-outs, board outline, stackup as structured data)
   - Routing intents per net class (width, impedance, layer preference, max skew)
   - Optional topological routing hints (rubber-band style: which side of which obstacles a route passes) — design the data model now, implement stretching later
3. **Compiler / sync layer**
   - `aipcb build` → generates `.kicad_sch` and `.kicad_pcb` deterministically (stable ordering, stable UUIDs derived from source paths, so diffs are minimal)
   - `aipcb check` → runs `kicad-cli sch erc` and `kicad-cli pcb drc`, parses their JSON report output, and re-emits violations as structured, source-referenced messages ("net USB_DP: skew violation" pointing to the YAML line/element that owns it)
   - `aipcb summary` / `aipcb query` → partial reads for token economy: dump one module with neighbors, all nets of a class, a one-line status per block
4. **Component database**
   - Local structured parts library (JSON/YAML): footprint ref (KiCad lib id), pinout with pin roles, electrical limits, optional supplier fields
   - Map to KiCad symbol/footprint libraries; validate that every `part:` resolves

### Technical constraints & choices

- Language: **Python 3.11+**, packaged with `pyproject.toml`, CLI via `typer` or `click`
- KiCad interop: target **KiCad 8/9** S-expression formats. Prefer writing the S-expression files directly (use or vendor a small S-expr serializer; evaluate the `kiutils` library first — if it's maintained and covers what we need, use it, otherwise write a minimal emitter)
- Use `kicad-cli` (assume it's on PATH) for ERC/DRC/Gerber — do not link against KiCad's Python API, so the toolchain runs headless in CI
- Everything deterministic: same source → byte-identical output. Add a test asserting this
- Git-friendliness is a hard requirement: sorted keys, no timestamps in output, UUIDs = hash(source path)
- Schema-validate the source format (JSON Schema or pydantic models) with helpful error messages — the AI agent will read them

### MVP milestones (implement in order, commit per milestone)

1. **M1 — Format & validation**: pydantic models for the semantic schematic layer, `aipcb validate`, 3 example designs (LED blinker w/ MCU, LDO supply, USB connector w/ diff pair), round-trip tests
2. **M2 — Schematic compile**: `aipcb build` emits a valid `.kicad_sch` that opens in KiCad and passes ERC via `kicad-cli sch erc` for the examples. Auto-placement of symbols can be naive (grid by module) — correctness over beauty
3. **M3 — Netlist + board skeleton**: emit `.kicad_pcb` with footprints placed per placement intents (simple force-directed or grid packing), board outline, stackup, net classes mapped to KiCad net classes
4. **M4 — Check loop**: `aipcb check` parses `kicad-cli` DRC/ERC JSON reports and maps violations back to source elements; output both human text and `--json`
5. **M5 — Query layer**: `aipcb query` subcommands for partial context extraction (module, net class, block summaries)
6. **M6 — Preserve & export**:
   - `aipcb build` becomes incremental: read any existing `.kicad_pcb`, and preserve manually-edited tracks/placements for elements whose source is unchanged (match via the deterministic UUIDs). Source changes always win for the elements they own; everything else survives a rebuild. Add tests for this
   - `aipcb export` runs `kicad-cli pcb export gerbers` + drill files into `out/`, so the full path source → fab data exists
7. **M7 — Topological (rubber-band) routing**. This is the flagship feature: routes are stored in source as *topology* (an ordered list of which pins/vias/obstacles a route passes, and on which side — sketch representation), and a deterministic "stretcher" converts topology into DRC-clean geometry. Emit ordinary KiCad tracks (45°/arc segments where KiCad supports arcs) so downstream tooling and fabs see nothing unusual. Build it in stages:
   - **M7a — Topology model & validation**: data model for per-net route topology (side-of-obstacle references, layer, via points as topological nodes). `aipcb route check`: verify topologies are realizable (no impossible crossings, planarity per layer via a triangulation of the placed board). Document the model in `docs/topology.md` with diagrams
   - **M7b — Naive stretcher**: for nets with a given (or auto-derived shortest) topology, generate geometry by rubber-band tightening: shortest path homotopic to the topology around obstacle hulls inflated by clearance. Respect track width and clearance from net classes. Output KiCad tracks; the result must pass `kicad-cli pcb drc` for the example boards. Start single-layer signal + via-hops; no length matching yet
   - **M7c — Auto-topology**: for unrouted nets, derive an initial topology automatically (route on the triangulation / detour graph, cost = length + congestion), then tighten. This gives `aipcb route all` for simple boards end-to-end
   - **M7d — Constraints**: differential pairs (coupled tightening, gap from impedance spec), max-skew and length-matching by meander insertion in the slack regions the topology allows, layer preferences. DRC-clean remains the acceptance bar
   - Suggested references for the approach: Dai/Kong/Leong's work on rubber-band sketch routing (SURF), Maley's homotopic routing theory, and the TopoR papers. Read up before designing M7a; write `docs/decisions/000x-routing-approach.md` summarizing the chosen algorithm
   - Incremental re-tightening is the payoff: when placement changes, topologies stay valid and only tightening re-runs. Make `aipcb route` idempotent and fast on unchanged nets

### Quality bar

- Type hints everywhere, `ruff` + `mypy` clean, `pytest` suite including golden-file tests for generated KiCad output
- A `README.md` explaining the vision, layer model, and a 5-minute quickstart
- `docs/format.md` — full source-format reference with examples
- CI-friendly: everything must run headless; skip gracefully with a clear message if `kicad-cli` is absent

### First actions

1. Verify `kicad-cli` availability and inspect its ERC/DRC report JSON format empirically (generate a tiny board to test against if needed)
2. Evaluate `kiutils` (or alternatives) for S-expression handling; write a short `docs/decisions/0001-kicad-io.md` ADR with your choice
3. Scaffold the repo, then start on M1

### Guardrails

- Ask me before deviating from the layer architecture; smaller implementation decisions are yours — record them as ADRs in `docs/decisions/`
- M7 is research-heavy: keep it strictly behind M1–M6. Do not let routing work leak into earlier milestones beyond the data-model stubs
- For M7, prefer well-understood computational geometry (constrained Delaunay triangulation, funnel/rubber-band shortest homotopic paths) over inventing new algorithms; use `shapely`/`scipy` where they fit, and keep the stretcher pure-functional (topology + placement in → tracks out) so it is testable and deterministic
- Acceptance for every routing stage is the same: generated tracks pass `kicad-cli pcb drc` on the example boards, byte-stable across runs

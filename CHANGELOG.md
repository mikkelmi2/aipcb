# Changelog

Notable changes, newest first.

## Unreleased — M19, 2026-08-24

Part 3 of the toporouter work. M18 found that the tightening *algorithm* is 0.039
profiled seconds of a 30-second route and that what the `tighten` stage actually
spends its time on is building — and discarding — the geometry tightening is done
against. This milestone builds the board that work should be measured on, then takes
the two cheap candidates the survey ranked. **Two of the four missed their budgets
and the report says so rather than re-reading the rule.** See
[`docs/reports/m19.md`](docs/reports/m19.md).

### Added

- **`examples/backplane` — the congestion stress board** (M19s), and the first
  example in this repository that does not finish for a reason that is arithmetic
  rather than topology. A six-slot instrument backplane whose card guides leave a
  4.0 mm channel between adjacent card positions: sixteen bus lines at 0.45 mm plus
  the rail and the return at 0.6 mm each put **8.4 mm of demand on a cut the two
  signal layers score at 7.6 mm**, seven times over. 168 connections, 110 routed,
  **58 handed over with the cut that stopped each one named**, five negotiation
  iterations without converging. **The router's capacity arbiter fires for the first
  time** — 132 refusals, the worst cut 3.77 mm of B.Cu against eleven nets wanting
  4.95 mm — where M17a found not one span rejected on capacity across the whole
  corpus. Byte-stable, DRC-clean on what it routes, and registered in
  `UNROUTABLE_EXAMPLES` because it was built to be over capacity.
- **`CONN_HDR_1X20`** in `examples/library/connectors.yaml` — a 20-pin single-row
  2.54 mm header.
- **`tests/test_routing.py::TestPointLocation`** — the vectorised candidate test
  against the scalar loop it replaced, on shared edges, shared vertices, swapped
  triangle indices and a real board's whole triangulation.

### Changed

- **`Triangulation.locate_many` computes its point-in-triangle test over numpy
  arrays** (M19a), where M17c had batched the R-tree query and left a scalar loop
  calling `_inside` 3 140 252 times and `_sign` 9 426 354 times on one board. Same
  arithmetic in the same order, so the signs are bit-identical; the tie-break —
  lowest triangle index wins — is written explicitly rather than hoped for.
  `_via_sites` 12.37 → 5.22 s and `_repair` 16.34 → 9.76 s profiled on
  `examples/pcie-sata`. **Every board hash is byte-identical.**
- **The corpus grew from eleven boards to twelve** and the baseline moved twice, in
  its own commit each time: 29.476 s of routing over eleven boards, then 167.036 s
  over twelve with `backplane` at 82 % of it, then 161.798 s after M19a. Copper,
  vias, layer changes and completion are unchanged on every original board
  throughout.
- **The capability-ladder extrapolation more than doubles, and that is the twelfth
  board rather than a regression** (M19d). Over the eleven original boards the
  exponents did not move again — 1.70 → 1.67 against connections, 1.10 → 1.09
  against connections × cuts — but the eleven-board fit under-predicts `backplane`
  by a factor of 3.9, and refitting over all twelve gives 1.87 and 1.03 and a
  900-connection estimate of **22.5 minutes** against M17's 11.3. The old number was
  fitted entirely below the range it was being used to predict; the new one is
  fitted through exactly one point above it, so it travels with that caveat
  attached and is provisional until the curve has more points between 90 and 900
  connections.

### Documented

- **Budgets re-anchor after every landed candidate** — a new standing rule in
  [`CLAUDE.md`](CLAUDE.md#budgets-re-anchor-after-every-landed-candidate), written
  because M19's two misses were both budget-anchoring failures rather than technique
  failures. Each candidate is measured against HEAD at its own start, and a budget
  that targets a specific cost is quoted in absolute seconds on named functions
  rather than as a percentage of a corpus the same milestone may change.
- **The benchmark's two layers**, in [`bench/results/README.md`](bench/results/README.md):
  the CI smoke run stays three boards and a handful of seconds, and `backplane` is
  in the full run only, because a tripwire nobody runs is not a tripwire.

### Not done, deliberately

- **M19a missed its 10 % budget** — it lands 2.9 % of the corpus and 8.3 % of the
  eleven boards the budget was drafted against — because `backplane` landed first
  and spends its time in `triangulate_free`, not in point location. The code is kept
  and [`docs/reports/m19.md`](docs/reports/m19.md) §2.3 flags that as a decision
  rather than a rule.
- **M19b was built, measured and rejected.** Bounding the private repair field lands
  1.0 % of the corpus against a 25 % budget, and it moves `pcie-sata`'s copper by
  12.5 mm — so the premise on which it was scheduled ahead of the fab round did not
  hold. Reverted; the design and the numbers are in
  [`docs/notes/routing-literature.md` §6](docs/notes/routing-literature.md#6-measured-results-as-the-closure-rule-requires).
  The finding worth carrying: **M19a and M19b were priced additively off one profile
  and they overlapped**, and a bounded search does not fail when its box is too
  small — it succeeds with something absurd.
- **M19c is not started.** Its gate opens on the measurement, and the stress board's
  profile says its prize is *larger* than the survey priced it: **64 % of
  `backplane`'s routing time is `triangulate_free`** and 39 % is one GEOS call
  inside it. The sequencing rule puts it after the fab round, and that has not
  happened.

## Unreleased — M17, 2026-08-24

Part 2 of the toporouter work: the three candidates the M16 baseline had already
ranked, each with a runtime budget stated before it was built and each landed
against `aipcb bench --compare`. No literature-derived techniques — those are M18.
See [`docs/reports/m17.md`](docs/reports/m17.md).

### Added

- **A via-reclaiming post-pass** (`route/tidy.py`). Every span where a connection
  leaves a layer and comes back to it, and every connection whose two pads share a
  layer, is re-tightened as a single leg and kept only if it comes out with **fewer
  vias and no more copper** — with M16a's special-cuts-corrected cut model as the
  congestion arbiter. Deterministic, order-stable and idempotent. Controlled-impedance
  classes and coupled pair legs are excluded by class, because a via removal changes
  the geometry M11d and M11e measured. Reported in `route all --json` as
  `routing.reclaimed`, and timed as a `reclaim` stage in `aipcb bench`.
- **`Triangulation.locate_many`** — the same answer as `locate` for a list of points,
  in one R-tree query.
- **`RoutedConnection.open_pads`** — which pads a connection was tightened against,
  recorded rather than re-derived, so a later pass re-tightens it in the same free
  space the router used.

### Changed

- **The router is roughly twice as fast, with every board's output unchanged.**
  Corpus `router_seconds` 61.9 → 29.9; tightening time −55.5% on `examples/pcie-sata`
  and −52.3% on `examples/mcu-4layer`. The profile found that two thirds of the
  "tightening" time was the *field builder*, rebuilt per repaired connection, and
  inside it a scalar Shapely loop over candidate via sites: 1.9 million `Point`
  constructions and 854 000 boundary derivations on one board. Those calls are now
  batched, `inflate` is memoised, and each layer's geometry is built once per
  connection rather than once per leg. No algorithm changed; all eleven board hashes
  were identical at this point.
- **`examples/pcie-sata` carries four fewer vias and 30.285 mm less copper**, and the
  `route-doubles-back` warning M16b found on it is gone. GND's `U1.17>U1.49` used to
  travel four millimetres, hop to B.Cu for half a millimetre, hop back and retrace
  its own path home; it is now a single 3.519 mm leg. The M16b guard is retained with
  its assertion inverted, and `route-doubles-back` is removed from the check loop's
  known issues rather than silenced.
- **`bench/results/baseline.json` moves to this milestone's commit.** Less copper,
  fewer vias, one fewer self-crossing, half the runtime; the M16 file stays in git
  history and every figure it is compared against is in the M17 report.

### Fixed

- **The 900-connection extrapolation.** Re-fitted after the above: roughly 11.3
  minutes where the same method on the M16 baseline gives 23.7. The exponents did not
  move (1.72 → 1.70 against connections, R² 0.94), which is the point — M17 harvested
  constant factors and left the shape of the curve for M18 to ask about.

## Unreleased — M16, 2026-08-23

Part 1 of what the [gEDA toporouter postmortem](docs/notes/toporouter-postmortem.md)
recommended: the guards and the instrument. None of the quality techniques, on
purpose — the study's central finding is that runtime, not correctness, is what
killed that router, and three of the five techniques trade runtime for quality.
See [`docs/reports/m16.md`](docs/reports/m16.md).

### Added

- **`aipcb bench`** — routes every bundled example and records wall clock per stage,
  completion, copper length, vias and layer changes against a computed lower bound,
  corridor utilization and headroom per layer, layer changes made without capacity
  pressure, and a hash of the board. `--compare` diffs two runs and exits 1 on a
  regression; `--smoke` runs the three-example CI subset.
  `bench/results/baseline.json` is the committed reference.
- **Second-diagonal ("special") cuts in the capacity check.** `check_capacity` now
  charges the other diagonal of every convex adjacent triangle pair — the cut a wire
  crosses when it rounds a triangle's apex without ever touching the diagonal.
  Measured at 4–29% of diagonals per layer having a shorter partner, so this is
  ordinary geometry rather than a corner case. [ADR
  0014](docs/decisions/0014-special-cuts.md). The router's own cost model is
  deliberately *not* charged for them; that is a measured trade for part 2.
- **A self-crossing invariant.** `route/invariant.py` now asks whether one
  connection's copper meets its own: `route-crosses-itself` (error) for a leg whose
  polyline is not simple, `route-doubles-back` (warning) for two legs of a
  connection meeting on one layer away from a join. It found a real one on
  `examples/pcie-sata` the day it landed — a GND connection laying eight millimetres
  of copper and two vias twice over. Reported in `route all --json` as
  `routing.self_crossings`.
- **A dated scale-robustness measurement.** `examples/led-blinker` produces identical
  copper at board origins of 100 mm, 2 147 mm and 50 000 mm, and routes differently
  at 100 000 mm — on Shapely 2.1.2 / GEOS 3.13.1. KiCad's whole coordinate range is
  ±2 147 mm, so the router is exact across twenty-three times it. The test holds both
  halves.
- **A CI `bench` job**, routing the smoke subset against the committed baseline on
  every pull request.
- **A part-2 plan in the roadmap**, with a runtime budget per candidate and the rule
  that a candidate which does not pay for itself is rejected *with its numbers
  recorded in the postmortem note*.

### Changed

- **The capacity check no longer claims more than it delivers.** `field.py`,
  `check_capacity` and [`docs/topology.md`](docs/topology.md) now say that the cut
  set is a **lower bound on congestion, not Maley's criterion in full**: a clean
  result is evidence, not proof. This was worth doing independently of charging the
  new cuts, and would have been done either way.

## Unreleased — M15.1, 2026-08-23

### Changed

- **The README and `docs/README.md` now state maturity per area instead of
  presenting the whole tool at one level.** The stable core leads — the source
  format and deterministic compilation, verification against real KiCad and the
  three invariants guarding it, schematic generation, and manual-layout support —
  followed by a maturity table and a plainly-marked beta tier. Nothing about the
  tool changed; what changed is that the page no longer implies that autorouting
  is as proven as the format under it.
- **`aipcb route` prints a one-line beta notice** on invocation, to stderr, with a
  link to the graduation conditions. Suppressed under `--json`: a machine consumer
  reads the same fact from the new `maturity` field instead.

### Added

- **`maturity: "beta"` in the `aipcb route --json` report** (`routing.maturity` for
  `route all`, `routes.maturity` for `route check`), so an agent reading the report
  can see the label structurally rather than parsing prose for it. No other key
  changed, added or moved; `tests/test_routing.py::TestBetaNotice` pins that.
- **Graduation conditions** for both beta labels, in
  [`docs/roadmap.md`](docs/roadmap.md#maturity-and-graduation): what has to be
  measured for each to come off, so that the label is a plan rather than a stamp.
- **A `Placement` section in the roadmap**, naming placement quality as the next
  obvious gap and why it matters to the router's own graduation.

## v0.1.0 — unreleased, tag pending

The first public version: the source format, deterministic compilation to KiCad 9,
ERC/DRC verification, schematic generation, topological autorouting, copper pours,
openEMS-backed SI simulation, and fabrication export — built across fifteen
milestones, each with a delivery report in [`docs/reports/`](docs/reports/).

**Not everything in it is at the same maturity**, and the tag should not be read as
saying otherwise. See the [maturity table](README.md#maturity-at-a-glance): the
format, compilation, `check` and schematics are stable; autorouting and SI
simulation are beta; the Freerouting bridge is new. `docs/reports/m15.md` records
what was measured before the repository was made public, including what the
measurements found that nobody was looking for.

# Changelog

Notable changes, newest first.

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

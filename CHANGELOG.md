# Changelog

Notable changes, newest first.

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

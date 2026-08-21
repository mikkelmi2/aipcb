# 0011 — Signal-integrity simulation: openEMS/gerber2ems environment verification

* **Status:** Accepted (phase 0 only — M12a/M12b/M12c not started)
* **Date:** 2026-08-21
* **Context:** milestone M12 phase 0, building on
  [ADR 0001](0001-kicad-io.md) (kicad-cli is the only writer of fabrication output),
  [ADR 0004](0004-board-generation.md) and [ADR 0007](0007-multilayer.md) (the
  stackup this tool synthesises)

## Context

M12 proposes to close the gap M11e left open: move from rule-based impedance
*assertions* to actual electromagnetic simulation, using openEMS (an FDTD field
solver) driven by Antmicro's gerber2ems, which takes standard fabrication files —
Gerbers, drill, stackup — as its input.

The milestone identifies the external toolchain as the biggest unknown and orders
it retired before any aipcb code is written. This ADR records that verification.
It answers four questions: does the toolchain install, does it reproduce the
vendor's own validated results, does it accept a board that `aipcb export`
produced, and how long does one pair take.

Everything below was measured on 2026-08-21 on the development machine
(16 cores, 30 GB RAM, Linux 7.0.0-29-generic). No repository file other than this
ADR was touched; all work was done in a scratch directory.

## Decision: build the container locally from a pinned Dockerfile

The milestone prefers "the container route from the gerber2ems repository over
building from source". That framing turns out to be a false choice: **Antmicro
publishes no prebuilt image.** The repository's `Dockerfile` *is* the container
route, and it builds openEMS from source inside the image. So the decision is not
container-vs-source but *where* the source build happens, and the container is
strictly better — it isolates a large C++/Qt/VTK dependency tree from the host and
from this project's Python environment.

No container runtime was present on the machine. `podman` was installed from the
distribution archive rather than Docker, because rootless podman needs no daemon
and no privileged group, so nothing about the host's security posture changes.

> **Note for the maintainer:** this installed `podman`, `uidmap`, `slirp4netns`
> and `fuse-overlayfs` system-wide via `apt`. Nothing was added to the project's
> `.venv`, and neither `pyproject.toml` nor any lockfile was modified.

### The exact pin

| Component | Pin | Source of the pin |
|---|---|---|
| gerber2ems | `9eaf3033f8adb0b468045f7177523162b388b020` (2026-04-23, `main`) | HEAD at clone time |
| openEMS-Project | `a30587728affa4f8451e13819981899bd8ab6b64` | Pinned by gerber2ems's own `Dockerfile` **and** named in its README as "the latest one tested with gerber2ems" |
| Base image | `docker.io/library/debian:trixie` @ `sha256:d8f17b92dc7ff10f9c1fdecab0ad21103d1d24aed823c3a0359e4f50adfab3eb` | Resolved at build time |
| Built image | `localhost/gerber2ems:phase0`, config ID `sha256:e1921ec2b3fc9e99216b075f65328b3022d424f9486a4da0b54ca0627f4d23b5` | Local build |
| podman | `5.7.0` (`5.7.0+ds2-3build1`) | Distribution archive |

Versions the built image reports at runtime: openEMS `v0.0.36-142-g32c5c6b`,
CSXCAD `v0.6.3-100-gd7d70ef`, hdf5 `1.14.5`, tinyxml `2.6.2`, boost `1_83`,
vtk `9.3.0`, Python `3.13`.

**Image build wall-clock: ~7 minutes** (apt dependency install ~3 min, openEMS
compile + Python bindings ~3.5 min, gerber2ems install ~20 s) on 16 cores.

Note the base image is pinned by *tag*, not digest, in the upstream `Dockerfile`.
The guardrail "the external toolchain is pinned (container digest or commit hash
recorded in the ADR and in a lockfile the repo carries)" is therefore **only
partly satisfiable as upstream ships it**: rebuilding from the same Dockerfile on a
different day resolves `debian:trixie` to a different base and re-runs `apt` against
moving archives. If M12 needs bit-reproducible simulation, the repo must carry its
own Containerfile that pins the base by digest. Recorded as an open question below.

### One build trap worth recording

Podman defaults to the OCI image format, which **silently ignores the `SHELL`
directive**. The upstream Dockerfile sets `SHELL ["/bin/bash", "-c"]` and then
relies on `pushd`, `popd` and `source` — all bash builtins absent from `/bin/sh`.
Building without `--format docker` produces a warning rather than an error and then
fails several minutes later. Any aipcb tooling that shells out to a build must pass
`--format docker` (or use Docker).

## Step 2 — the vendor's bundled examples run, and match their VNA data

The repository ships six examples, slices of Antmicro's open-hardware Signal
Integrity Test Board; four carry real VNA measurements for comparison.
`stub_short` was run with `gerber2ems -a` (geometry, simulate, postprocess).

**Result: pass. 40.6 s wall clock**, 361,284 FDTD cells, 69,600 timesteps,
745 MCells/s. All expected outputs produced (per-port CSV, S-parameter plots,
Smith chart, impedance plots).

Simulated port impedance against the shipped `vna.csv`:

| Frequency | \|Z\| simulated | \|Z\| measured | Deviation |
|---|---|---|---|
| 500 MHz | 43.97 Ω | 44.45 Ω | −1.1 % |
| 1 GHz | 43.89 Ω | 40.23 Ω | +9.1 % |
| 2 GHz (stub resonance, dip) | 20.27 Ω | 19.07 Ω | +6.3 % |
| 5 GHz (stub resonance, peak) | 332.02 Ω | 337.69 Ω | −1.7 % |
| 6 GHz | 50.81 Ω | 54.40 Ω | −6.6 % |

The solver reproduces both the flat region and the *positions and magnitudes of the
stub resonances* — the 5 GHz peak at 332 Ω against a measured 337 Ω is a demanding
test and it passes to −1.7 %. Percentage error is large near 3 GHz (−25 %) purely
because |Z| is sweeping through hundreds of ohms there, so a small frequency shift
reads as a huge relative error; that is an artefact of the metric, not of the
solver. **The toolchain is sound.** Any inaccuracy found later is ours, not
openEMS's — a conclusion worth having before debugging our own integration.

## Step 3 — the compatibility test against an aipcb-exported board

This is the make-or-break step. `aipcb export examples/diff-pair/design.yaml` was
run into a scratch directory and the output compared, file by file, against what
gerber2ems looks for.

### What gerber2ems requires

Working directory contains `simulation.json` and a `fab/` subdirectory holding:

| Input | Discovery rule (from the source, not the README) |
|---|---|
| Edge cuts Gerber | `fab/*Edge_Cuts.gbr` — also used to crop every other layer, so it defines the coordinate origin |
| Copper Gerbers | `fab/*_Cu.gbr`, named `<anything>-<user-name-from-stackup>.gbr` |
| Stackup | `fab/stackup.json`, `format_version` `1.0`; per layer `name`, `type`, `color`, `thickness`, `material`, `epsilon`, `lossTangent`, `user-name` |
| Drill | `fab/*-PTH.drl`, Excellon, **non-negative coordinates relative to the board's bottom-left corner** |
| Port positions | `fab/*pos.csv`, KiCad CSV layout, rows whose `Package` contains `Simulation_Port` and whose `Ref` is `SP<n>`; coordinates relative to the Edge.Cuts bounding-box corner |
| `simulation.json` | frequency range, `max_steps`, grid, ports (width/length/impedance/layer/plane/excite), `differential_pairs`; **all lengths in micrometres** |
| `netinfo.json` | optional, in the working directory; without it every net except GND is used for grid generation |

### What `aipcb export` produces today, and the gaps

`aipcb export` emitted 14 files. Against the list above:

| # | Requirement | Status | Detail |
|---|---|---|---|
| 1 | `*Edge_Cuts.gbr` | **OK** | `diff-pair-Edge_Cuts.gbr` |
| 2 | `*_Cu.gbr` per copper layer | **OK** | `diff-pair-F_Cu.gbr`, `diff-pair-B_Cu.gbr`; the `--no-protel-ext` naming already matches the `<text>-<user-name>` convention exactly |
| 3 | `fab/stackup.json` | **MISSING** | Never written. Derivable — see below |
| 4 | `*-PTH.drl` | **WRONG NAME** | `compile/export.py` passes neither `--excellon-separate-th` nor a separate PTH/NPTH split, so KiCad emits one `MixedPlating` file called `diff-pair.drl`. gerber2ems globs for `-PTH.drl`, finds nothing, and calls `sys.exit(1)` |
| 5 | Drill coordinates | **WRONG FRAME — silent** | See below. This is the dangerous one |
| 6 | `*pos.csv` | **WRONG NAME** | aipcb writes `positions.csv`. The glob is `*pos.csv`, which requires the name to *end* in `pos.csv`; `positions.csv` ends in `ions.csv` and does not match. No error is raised — the file is simply never seen |
| 7 | Simulation ports | **NOT A CONCEPT** | aipcb has no notion of a simulation port. Nothing emits `SP<n>` / `Simulation_Port` rows |
| 8 | `simulation.json` | **MISSING** | Expected; this is M12b's job, not an export gap |
| 9 | `netinfo.json` | **MISSING** | Optional, but aipcb knows every net and its geometry, so producing it is nearly free and improves grid quality |

#### Gap 5 in detail — the silent one

`aipcb export` passes `--use-drill-file-origin` for Gerbers and `--drill-origin
plot` for drill, both of which mean "relative to the drill/place file origin". But
**aipcb never writes an `(aux_axis_origin ...)` into the `.kicad_pcb`**, so that
origin is (0,0) and every file lands in absolute KiCad page coordinates. On
`examples/diff-pair` the board sits at x 100–160 mm, y 100–130 mm, so the exported
drill file contains lines like `X105.0Y-110.0`.

gerber2ems parses drill coordinates with
`re.fullmatch("X([0-9]+.[0-9]+)Y([0-9]+.[0-9]+)\n", line)` — a pattern that cannot
match a negative number. Every via would be **silently dropped**: no error, no
warning above debug level, just a board simulated without any of its vias. On a
board whose return path depends on stitching vias that is a wrong answer delivered
confidently, which is worse than a crash.

Two of the three fixes are one-line changes to the flags in
`src/aipcb/compile/export.py` (`--excellon-separate-th`; name the position file
`*-pos.csv`). The third — writing an aux origin at the board's bottom-left corner —
touches board generation and would change every exported Gerber, so per the
milestone's guardrail it needs its own schema/ADR pass with byte-stability checks
on the existing golden files.

#### Gap 3 in detail — the stackup is *derivable*, and better than expected

`aipcb` already synthesises a complete KiCad stackup into the `.kicad_pcb`
(`compile/board.py:_stackup`), including material, `epsilon_r` and `loss_tangent`
per dielectric. A shim transcribed it into a valid `stackup.json` in ~30 lines with
no information loss. `kicad-cli` has no stackup export, so this transcription is
work aipcb must do itself, but the data is there.

What is *not* there is any way for a designer to state it:

* **`loss_tangent` is hardcoded to `0.02`** in `compile/board.py:_dielectric`. It is
  not on the `StackupLayer` model at all, so no source file can set it. Dielectric
  loss is the dominant loss mechanism above ~1 GHz; for insertion-loss results this
  is the single most important number and it is currently unreachable.
* **`epsilon_r` is settable but unused.** `StackupLayer.epsilon_r` exists, yet
  `_dielectric` takes the *first* core/prepreg layer's value and applies it to
  *every* dielectric, and **not one of the ten examples populates `stackup.layers`
  at all** — every board runs on the built-in FR4 / εr 4.5 / tanδ 0.02 default.
* **Every dielectric is typed `core`**, even on the 4-layer board where a real
  stackup alternates core and prepreg with different thicknesses. Thickness is the
  board total split evenly.

None of this blocks phase 0, and the defaults are reasonable, but the milestone's
own honesty clause — "simulation accuracy depends on stackup data matching what the
fab actually builds" — is currently vacuous, because the source cannot express the
fab's stackup even if the engineer knows it.

#### Gap 10 — `aipcb export` does not route

Found while assembling the test and not on anyone's list: **`aipcb export` builds
and exports an unrouted board.** The exported `F_Cu.gbr` for `examples/diff-pair`
contains 16 pad flashes and **zero track draws**. The tracks exist only after
`aipcb route all`, and `export` builds into a fresh temporary directory, so a plain
`aipcb export` discards them.

The workflow that does produce copper is `aipcb route all -o DIR` followed by
`aipcb export --build-dir DIR`, which works because builds are incremental
(54 track draws in the Gerber, confirmed). M12 must not simply call `export`.
Whether shipping a fabrication package with no copper in it is acceptable
behaviour for `export` is a question for the maintainer, not for M12.

### Does it actually run?

Yes. With the gaps above bridged by a scratch shim, gerber2ems consumed the
aipcb-exported `examples/diff-pair` board: Gerbers converted to PNG, stackup
parsed, mesh built, FDTD executed. **Geometry-level compatibility is proven.**

Postprocessing then failed with a divide-by-zero, for a reason that is a finding
in itself: **`examples/diff-pair` has no reference plane.** Its B.Cu carries
nothing but through-hole annular rings — no pour, no plane. A gerber2ems port is a
microstrip port defined between a signal layer and a `plane` layer; with no copper
on the reference layer, `Z_ref` is zero and the S-parameter extraction divides by
it. The board is not a simulatable controlled-impedance structure.

To separate "aipcb's export format is incompatible" from "this particular example
has no plane", a **slice** was then generated from the same aipcb-routed geometry —
the two `DIFF_P`/`DIFF_N` traces extracted from aipcb's own `F_Cu.gbr`, a solid
reference plane on B.Cu, a tight outline, and four ports at the trace ends. This is
what M12a will build properly.

**That ran end to end**: energy decayed to −70 dB, all four S-parameter sets,
differential impedance and delay plots produced. So:

> **The format incompatibilities are mechanical and fixable. There is no deep
> incompatibility. The blocking problem is the aipcb example boards themselves.**

## Step 4 — measured wall-clock for one pair

One pair means four ports and two excitations, timed end to end (geometry
generation + both FDTD runs + postprocessing), inside the container.

| Case | Cells | Wall clock |
|---|---|---|
| Vendor example `stub_short` (1 trace, 2 ports) | 361,284 | **40.6 s** |
| diff-pair, whole 60 × 30 mm board, default grid | 380,640 | 46.5 s (postprocess failed — no plane) |
| diff-pair **pair slice**, default grid | 105,000 | **31.3 s** |
| diff-pair pair slice, refined grid (`inter_layers` 24, `optimal` 25 µm) | 652,916 | **116.6 s** |
| diff-pair pair slice, refined grid, 150 Ω ports | 652,916 | 103.6 s |

**Headline: 30 s to 2 minutes per pair** on 16 cores at these board sizes, dominated
by mesh density, not by trace length. This is far cheaper than the milestone's
"minutes to hours" assumption and it changes the planning: per-pair caching is still
worth having, but a full pcie-sata batch is plausibly a few minutes, which is a CI
stage rather than an overnight job. openEMS sustained 750–1270 MCells/s.

The caveat is that these are *slices*, which is the whole point of M12a. The
unsliced 60 × 30 mm board already needed 380,640 cells at the coarsest useful grid.

## Step 5 — verdict

**Phase 0 passes.** The toolchain installs from a pinned source, reproduces the
vendor's VNA-validated example to within a couple of percent off-resonance, accepts
aipcb-exported Gerbers and drill data, and completes a differential simulation of
aipcb-routed trace geometry in about two minutes. Nothing found is a reason to
abandon the approach or to prefer a different solver.

M12a may proceed, with one condition and one warning.

### The condition: the impedance numbers are not yet trustworthy

Three port and mesh configurations of the *same physical geometry* produced
differential impedances of ≈26 Ω, ≈340 Ω and ≈950 Ω. The coarse-grid run also
produced |Sdd21| > 1 — energy gain, which is non-physical and a definitive marker
that the extraction is broken rather than merely imprecise. The refined grid fixed
that (|Sdd21| ≤ 0.83), but the absolute values still moved by 3× when only the port
termination changed, which a converged result must not do.

The solver is not at fault — it matched the vendor's VNA data on the vendor's
board. The difference is the *structure*: every gerber2ems example places its
reference plane 0.12 mm below the trace, whereas `examples/diff-pair` is a 2-layer
1.6 mm board whose nearest copper is 1.51 mm away. A 0.25 mm trace 1.51 mm above
its return is barely a guided structure at all; it radiates, so no port calibration
converges. **M12a must not report an impedance number until a known-good structure
reproduces a known-good value.**

### The warning: the flagship example cannot hit its own target

`examples/diff-pair` declares `impedance_ohm: 100.0`. On its declared stackup that
is unreachable, and not marginally so. With w = 0.25 mm over the 1.51 mm dielectric
that aipcb's own stackup arithmetic derives from `thickness_mm: 1.6`, the
Hammerstad closed form gives a single-ended Z₀ of **≈135 Ω**, so a loosely coupled
pair lands near 270 Ω. Every simulation run agreed on the direction even where they
disagreed on the value: the pair is in the hundreds of ohms, never near 100.

The design file's own comment — "estimated, because the fabricator's stackup is the
thing that decides it" — is exactly right, and this is precisely the class of defect
M12 exists to catch. It is also a caution about the milestone's acceptance criterion
that diff-pair should land "within plausible range of its design target": **it will
not, and it should not, because the design target is wrong.** The right outcome is
for M12 to say so.

The stackup that *is* worth simulating is `examples/mcu-4layer`, whose In1.Cu is a
GND plane 0.48 mm below F.Cu — a real microstrip reference, and the geometry
pcie-sata will resemble. M12a should calibrate there, not on diff-pair.

## Consequences

* aipcb shells out to a pinned container; openEMS is never a host dependency and
  never enters `.venv`. The fallback architecture the milestone permitted is the
  primary one, and it is a good outcome rather than a concession.
* The container must be built with `--format docker`, or with Docker.
* Four export fixes are needed before M12a can consume `aipcb export` directly:
  the PTH drill split, the position-file name, the aux origin, and `stackup.json`.
  The first two are flag changes; the aux origin changes every existing Gerber and
  so needs its own byte-stability pass; `stackup.json` is a new output derived from
  data aipcb already computes.
* Two of the four failure modes found are **silent** — the position file and the
  drill coordinates are both discarded without an error. Whatever aipcb builds must
  assert that gerber2ems saw the ports and the vias it was given, rather than
  trusting a zero exit code.
* Simulation is per-slice, not per-board, in the code as well as in the plan: the
  full 60 × 30 mm diff-pair board is 3.6× the cells of its own pair slice, and that
  ratio grows with board size.

## Open questions

* **Reproducible pin.** Upstream pins its base image by tag and installs from
  moving apt archives. Carrying our own Containerfile with a digest-pinned base is
  the only way to satisfy M12's pinning guardrail. Worth doing, but it means
  maintaining a fork of Antmicro's build.
* **Where do stackup materials come from?** `loss_tangent` is unreachable from
  source and `epsilon_r` is effectively ignored. Simulation is only as honest as
  these numbers, so a `stackup.layers` block that real designs actually populate —
  and a per-dielectric core/prepreg distinction — looks like a prerequisite for
  M12c's results to mean anything.
* **Port impedance and calibration.** Ports need a reference impedance derived from
  the net class rather than a fixed 50 Ω, and M12a needs a convergence check
  (refine the mesh, confirm the answer stops moving) before any verdict is emitted.
* **Antmicro's `kicad-si-simulation-wrapper`** was not evaluated. It targets
  KiCad 9 and generated these very examples, so the M12a reuse-vs-reimplement call
  is still open. Its slicing works from a `.kicad_pcb`, which aipcb has; but aipcb
  knows net roles (`role: ac_coupling`) that a geometric slicer cannot, which is
  the milestone's stated reason to prefer its own.
* **Does `aipcb export` shipping an unrouted board need fixing on its own merits?**
  Out of scope for M12, but it is a surprising default for a command whose stated
  purpose is producing a package a board house can quote from.

## What was not done

Phase 0 only. No aipcb source, test, example or existing document was modified; no
`aipcb simulate` command exists. The shim that bridged the export gaps lives in the
scratch directory and is deliberately not repository code — its value is the gap
list above, which M12a should implement properly.

`examples/pcie-sata` was not exercised: M11 was running concurrently and its report
did not yet exist. All figures here are from `examples/diff-pair`.

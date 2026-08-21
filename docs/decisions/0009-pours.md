# 0009 — Copper pours: who fills the zones

* **Status:** **Accepted — Option 1**, implemented in M10, decided by the project owner on 2026-08-21
  with the conditions recorded under "The decision" below. M10 resumed on that
  basis; this ADR amends [ADR 0001](0001-kicad-io.md).
* **Date:** 2026-08-21
* **Context:** milestone M10 ([`docs/milestones/m10-pours.md`](../milestones/m10-pours.md)),
  building on [ADR 0001](0001-kicad-io.md)

## Why this ADR exists before the milestone does

M10's specification opens with two empirical questions to settle *before writing
code*, and closes with a guardrail:

> If KiCad's CLI cannot fill zones headlessly in the installed version, stop and
> present options before working around it.

The questions were measured. The answer to the first one trips the guardrail, so
this ADR records the measurements and the options rather than a decision. The
measurements are the durable part: whoever resumes M10 should not have to repeat
them.

All figures below were taken on 2026-08-21 against **KiCad 9.0.8** (`kicad-cli
version` → `9.0.8`, Debian package `kicad 9.0.8+dfsg-1`).

## Finding 1 — `kicad-cli` 9.0.8 cannot fill zones, by any route

`kicad-cli` exposes six top-level commands (`fp`, `jobset`, `pcb`, `sch`, `sym`,
`version`). Under `pcb` there are exactly three: `drc`, `export`, `render`. There
is **no fill or refill subcommand, and no fill flag on any of them**. The jobset
runner does not add one: the complete set of job-type identifiers compiled into the
9.0.8 libraries is `pcb_drc`, `pcb_render`, `pcb_netlist`, ten `pcb_export_*` jobs,
`sch_erc` and six `sch_export_*` jobs. Nothing that fills.

The one place `ZONE_FILLER` appears anywhere in the shipped libraries is as its
SWIG binding (`delete_ZONE_FILLER`, …) — that is, the Python API is the only
surface KiCad 9.0.8 exposes it through at all.

That is an absence, so it was tested positively rather than inferred from `--help`.

**The probe.** A 40 × 30 mm board carrying two vias 24 mm apart on net `GND`, no
track between them, and one unfilled `GND` zone spanning the board. If a stage
fills the zone, the two vias become connected and KiCad's unconnected count drops
from 1 to 0. It is a yes/no question with a yes/no answer.

| Stage | Unconnected items | Zone filled? |
|---|---|---|
| `kicad-cli pcb drc --severity-all` on the unfilled board | **1** | no |
| the same, with the zone's `(fill yes …)` flag set | **1** | no |
| after filling the zone (see Finding 2), `kicad-cli pcb drc` | **0** | yes |

The gerber path answers the same way. Plotting `F.Cu` from the unfilled board
gives a 596-byte file with 2 coordinate records; from the filled board, 1066 bytes
with 20. `kicad-cli pcb export gerbers` **plots whatever fill data is in the file
and never regenerates it** — an unfilled zone exports as no copper at all, silently.

So both halves of M10b's plan — fill before DRC, fill before gerbers — have no
`kicad-cli` surface to stand on. This is the guardrail condition, met exactly.

## Finding 2 — KiCad's fill engine *is* reachable headlessly, but not from the CLI

The same KiCad package ships `pcbnew` as a Python extension module
(`/usr/lib/python3/dist-packages/_pcbnew.so`), and it exposes `ZONE_FILLER`:

```python
board  = pcbnew.LoadBoard(src)
filled = pcbnew.ZONE_FILLER(board).Fill(board.Zones())
board.BuildConnectivity()
board.Save(dst)
```

This runs **with no display, no X server and no KiCad configuration**, and it is
KiCad's own fill engine — not a reimplementation, which M10 rightly rules out. On
the real `examples/usb-port` board (24 connections routed, 92 segments, 4 vias)
with `GND` poured on `F.Cu` and `B.Cu`, it produces 444 additional polygon
vertices in **0.42 s**, and the result passes `kicad-cli pcb drc --severity-all`
with **0 violations and 0 unconnected items**.

So the milestone is entirely achievable. The blocker is not capability; it is
which door we are allowed to open.

## Finding 3 — the fill is byte-deterministic (M10's "before writing code" step 2)

The stability policy in M10b depends on this, and the specification says to
measure it rather than assume it. Measured, on two boards, filling the *same*
input repeatedly in *separate processes*:

| Board | Runs | Distinct output hashes |
|---|---|---|
| synthetic probe (1 zone, 2 vias) | 5 | **1** |
| `examples/usb-port` (2 zones, 31 pads, 92 segments, 4 vias) | 3 | **1** |

> **Superseded in part by Finding 5.** Re-measured on the shipping implementation,
> the *fill geometry* repeats exactly but the whole file does not: KiCad's writer
> invents UUIDs for properties it adds. The stability policy is stated in the
> stronger form because of it.

Byte-identical every time, whole file, not merely the fill polygons. Eight runs is
not a proof of determinism across all inputs — thermal spoke placement and island
removal are the plausible places for tie-breaking to leak — but there is no
evidence of nondeterminism, and the stability policy M10b proposes (guarantee
covers unfilled build output; filled output stable in practice) is consistent with
what was measured.

## Finding 4 — per-pad-instance keying survives the shared-UUID defect

M10 requires per-pad-instance keying in two places: the `thermal`/`solid`
connection-style override, and stitching-via obstacle checks. `docs/roadmap.md`
records a standing defect that appears to threaten both — **pads that share a
number share a UUID**. Verified before relying on anything:

The defect is real and worse than it sounds. `examples/usb-port`'s board emits
**31 pads carrying only 20 distinct UUIDs**; the Micro-B receptacle's twelve pads
numbered `6` all carry the same UUID, twelve times over.

It nonetheless blocks **neither** M10 requirement, for two independent reasons:

* **aipcb's internal keying does not go through the UUID.**
  `route.obstacles` resolves a pad reference to a concrete instance with a
  `reference#index` key (`J1.6#7`, `J1.6#9`, …), built while walking the
  footprint's pad list. Those keys are already distinct for the colliding pads —
  they show up in the router's own output — so stitching-via obstacle checks
  inherit correct per-instance keying for free.
* **KiCad's per-pad zone-connection override is positional, not referential.**
  `zone_connect` is a token *inside* the pad's own s-expression — stock KiCad
  footprints such as `L_Coilcraft_LPS4018.kicad_mod` carry it — so a `thermal_pad`
  role can be applied to one specific pad instance by writing the token into that
  pad node, with no UUID lookup anywhere.

What the collision does block is unchanged and already recorded: a DRC violation
landing on one of twelve identically-identified pads cannot say which. That is the
existing roadmap item, out of scope here, and M10 does not make it worse.

## The options

Presented, not chosen. Options 1 and 2 both need a decision that reverses or
qualifies [ADR 0001](0001-kicad-io.md), which excluded `pcbnew` **up front, by the
brief** — "it forces a KiCad runtime into CI and cannot run headless cleanly" —
and which `kicad/cli.py` still states as a design property in its module docstring:
"KiCad is the backend of this toolchain, not a library dependency."

Note that half of ADR 0001's stated reason is now empirically stale: on 9.0.8
`pcbnew` *does* run headless cleanly (Findings 2 and 3 were measured with no
display). The other half stands unchanged — it does force a KiCad runtime into CI,
where today only the `kicad-cli` binary is needed.

**Option 1 — drive `pcbnew` in a subprocess.** A small script invoked as
`/usr/bin/python3 -m aipcb.kicad.fill`, alongside `kicad/cli.py` rather than inside
it. Keeps the venv clean of KiCad (it is currently `include-system-site-packages =
false`, and `pcbnew` lives in the system `dist-packages`) and keeps the failure
mode legible: no `pcbnew`, no fill, clear message. Costs a second Python
interpreter per fill and a documented CI requirement of the `kicad` package rather
than just `kicad-cli`. *Convenient accident of this machine: system Python and the
venv are both 3.14.4, so the same interpreter can load `pcbnew`.*

**Option 2 — import `pcbnew` in-process.** Flip `include-system-site-packages`, or
add the `dist-packages` path. Cheapest at runtime, and the most direct
contradiction of ADR 0001; it also couples the venv's Python version to whatever
KiCad was built against, which is luck today and a breakage on the next KiCad
upgrade.

**Option 3 — descope M10 to unfilled zones.** Emit zones as source intent (M10a),
skip fill, and defer M10b/M10d. Honest but thin: DRC over an unfilled pour checks
nothing about the pour, gerbers would ship with no plane copper at all, and the
plane-integrity report has no filled polygons to analyse. It would also make
"0 DRC violations" a weaker claim than it is today, which is the wrong direction.

**Option 4 — wait for, or petition, a `kicad-cli` fill command.** Correct in the
long run and useless now.

**Not an option: reimplementing zone fill in aipcb.** M10 rejects it explicitly and
this ADR records the rejection as asked. KiCad's fill is what DRC checks against;
a second implementation would be checked against the first and would differ, and
the difference would be our bug on every board.

## Recommendation

**Option 1**, if the owner is willing to amend ADR 0001. It is the only option that
delivers M10 as specified while keeping KiCad's own engine as the reference, and
Findings 2 and 3 show it working, fast and deterministic. It should be recorded as
a deliberate amendment to ADR 0001 — narrowing "no `pcbnew`" to "no `pcbnew` in the
`aipcb` package's own process" — rather than as a quiet exception, because the CI
requirement changes for everyone.

The decision is the owner's. M10 stopped here rather than pick.

## The decision

**Option 1 is approved: drive `pcbnew` in a subprocess**, recorded here as a
deliberate amendment to [ADR 0001](0001-kicad-io.md) — narrowing "no `pcbnew`" to
"no `pcbnew` in the `aipcb` package's own process" — and not as a quiet exception.
Four conditions come with the approval; all four are binding on the
implementation.

### 1. Rationale

ADR 0001's exclusion is half-stale. Its two stated reasons were that `pcbnew`
"forces a KiCad runtime into CI" and "cannot run headless cleanly". Finding 2
measured the second one false on 9.0.8 — the fill ran with no display. The first
survives as a statement but not as a *cost*: the project already requires
`kicad-cli` on `PATH`, so a KiCad installation is a standing prerequisite, and
`pcbnew` ships inside it. The marginal cost of this decision is a system-python
subprocess call, not a new runtime.

What does not change: **reimplementing zone fill in `aipcb` stays rejected.**
KiCad's fill is what DRC checks against, so a second implementation would be
checked against the first, would differ, and the difference would be our bug on
every board. And Finding 3's determinism measurement — byte-identical output
across 8 runs on 2 boards — retires the last concern about depending on it.

### 2. Version lock

The subprocess **must verify that `pcbnew`'s version matches `kicad-cli`'s before
filling.** On mismatch it stops with a clear message naming both versions. There
are no silent cross-version fills: the whole point of using KiCad's engine is that
it is the same engine DRC checks against, and a `pcbnew` from a different install
than the `kicad-cli` running DRC quietly voids that.

### 3. This is a bridge, not a turn

This decision covers **the 9.0.8 situation specifically.** If a future KiCad ships
`kicad-cli pcb fill` or equivalent, the subprocess is replaced by it and this ADR
is superseded — the CLI is the preferred surface and remains so. **Re-measure at
each KiCad major**: Finding 1 is a measurement with an expiry date, not a permanent
property of the tool.

### 4. Subprocess hygiene

- **Explicit `python3` resolution, documented.** The interpreter that can import
  `pcbnew` is the system one; the project venv cannot (`include-system-site-packages
  = false`). How it is located must be written down, not inferred from `PATH` by
  luck. Note that system Python and the venv being the same 3.14.4 on the machine
  where this was measured is an accident, not something to rely on.
- **Structured error propagation.** A fill failure must surface as a *check
  failure* with `pcbnew`'s stderr attached. It must never silently produce an
  unfilled board — an unfilled pour exports as no copper at all (Finding 1), so a
  swallowed fill error is precisely the silent-corruption failure mode this project
  exists to eliminate.
- **A test that simulates `pcbnew` being absent**, asserting the failure is legible
  and loud.

## Implementation findings

Added while M10 was built on the decision above, on 2026-08-21 against the same
KiCad 9.0.8. Everything here was measured on the code that shipped, not on a probe.

### Finding 5 — the fill geometry repeats; the filled *file* does not

Finding 3 measured "byte-identical across the whole file, not merely the fill
polygons" on eight runs. Re-measured on the shipping implementation, filling
`examples/usb-port` five times in five separate processes, the answer splits in two:

| What was compared | Distinct results in 5 runs |
|---|---|
| the `filled_polygon` geometry | **1** |
| the whole file | **5** |

Exactly **12 lines differ** between any two runs, and all twelve are `uuid` tokens
of `Datasheet` and `Description` properties that KiCad's writer *adds* to footprints
that do not carry them, each with a freshly random identifier. Strip every `uuid`
token and the five files hash identically.

So the fill itself is deterministic — which is what the stability policy needed —
but a filled file is not byte-stable, and M10b's policy is stated in the stronger
form because of it: **the byte-identical guarantee covers unfilled build output.**
The earlier reading was too generous to a claim nothing downstream depends on.

Two smaller facts from the same measurement, both load-bearing:

* **No UUID we emit is lost.** 279 UUIDs go into the fill on `usb-port` and all 279
  come out, so `kicad-cli pcb drc` on the filled board still maps every violation
  back to source exactly. The twelve new ones are additions, not replacements.
* **`pcbnew` does not de-alias the duplicate pad UUIDs.** The 31 pads still carry
  20 distinct identifiers after a round trip, so Finding 4's defect is neither made
  worse nor quietly repaired by going through the fill.

### Finding 6 — KiCad's own thermal-relief default produces boards KiCad rejects

KiCad's zone dialog defaults to a 0.5 mm thermal gap and a 0.5 mm bridge. Emitting
those produced `starved_thermal` errors on four of the eight examples that gained a
pour: on a 1.7 mm through-hole pad at 2.54 mm pitch only one of the four spokes can
reach the plane, and KiCad 9's rule wants at least two.

Measured on `examples/routing-demo`, filling and running `kicad-cli pcb drc`:

| Thermal relief | `starved_thermal` errors |
|---|---|
| gap 0.5, bridge 0.5 (KiCad's dialog default) | 2 |
| gap 0.3, bridge 0.4 | 1 |
| gap 0.25, bridge 0.6 | 0 |
| gap 0.25, bridge 0.5 (**adopted**) | 0 |
| solid connection | 0 |

So `aipcb`'s default relief is a **0.25 mm gap with a 0.5 mm bridge**, and the
divergence from KiCad's dialog is deliberate and documented in `docs/format.md`: a
default that produces an error is not a default. A pour that wants KiCad's figures
still says so with `thermal_gap:` and `thermal_bridge_width:`.

The one case this does not fix is a pad whose relief is starved by *geometry* rather
than by numbers — a receptacle's shield tab at the board edge, where the pour is
clipped and a spoke has nowhere to go. That wants a solid connection, which is what
a shield tab wants anyway, and `pad_connect:` is how the source says so.

### Finding 7 — the per-pad override needs both "this pad" and "these pads"

Finding 4 established that the override has to be keyed per pad *instance*, because
a pad number is not an identity. Building it showed the other half: a reference that
names **only** one instance is not enough either. `examples/enclosure` needs all
twelve of a Micro-B's shield tabs flooded, and twelve `#N` lines to say so would be
a worse source format than the problem it solves.

So `U2.4` means every pad numbered 4 and `U2.4#2` means the second one alone, with
the suffix outranking the bare form. Both appear in the examples: `enclosure` floods
`J1.6`, and `usb-port` floods `J1.6#7` and leaves its eleven siblings thermal —
which is also the test that proves the instance keying works, since those twelve
pads share one UUID.

### Finding 8 — same-net copper may touch; same-net *holes* may not

The router's obstacle model correctly lets copper of one net overlap copper of the
same net, so a stitching via on `GND` is not blocked by a routed `GND` via. Holes
are a different rule: `mcu-4layer` put a stitching barrel **0.2104 mm** from a
routed via against a 0.25 mm minimum, and `kicad-cli pcb drc` said so.

Stitching therefore checks candidate positions against *every drilled hole on the
board* — vias and through-hole pads, whatever their net — at KiCad's 0.25 mm
hole-to-hole minimum, in addition to the copper-clearance check it inherits from the
router's obstacle model. Oval pad drills are measured across their long axis.

### Finding 9 — a stitching via has to land in copper, or it is a DRC violation

A via on `GND` that does not touch any other `GND` copper is an isolated island, and
KiCad reports it as an unconnected item. So every candidate position must lie inside
the net's pours on **both** layers the barrel joins, eroded by the via's own radius,
before anything else is checked. Stitching a net with no pour on one of those layers
places nothing and says why (`stitching-no-plane`).

This is also what makes stitching *useful* rather than decorative, measured on
`examples/qfn-fanout`: a 0.5 mm-pitch escape field cuts the back-side ground into
pieces, and a `starved_thermal` error landed on the receptacle's shield tab because
its spokes reached an island connected to nothing. Adding a 4 mm grid tied those
islands up to the front pour and the error went away — the board went from one DRC
error to none, with the plane-integrity report still honestly reporting five islands
on B.Cu.

### Finding 10 — two of the ten examples have no ground to pour

`examples/congestion` and `examples/overconstrained` declare four signal nets
(`SWAP_A`..`SWAP_D`) and nothing else. They are routing-topology fixtures, not
boards, and pouring one of their signal nets would be a fiction. They are the two
examples that did **not** gain a `pours:` block, which is recorded here rather than
worked around, and they are why the backward-compatibility claim has a live witness:
`congestion` still builds with no zone in its output at all.

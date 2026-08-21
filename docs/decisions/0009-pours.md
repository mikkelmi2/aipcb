# 0009 — Copper pours: who fills the zones

* **Status:** **Proposed — blocked.** M10 stopped before writing code and is
  waiting on a decision from the project owner. Nothing in this ADR is implemented.
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

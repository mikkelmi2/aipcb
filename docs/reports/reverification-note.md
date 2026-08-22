# Which recorded measurements could have been taken on an accumulated board

M13.5 §1.4 found that `aipcb check` was not a function of its source. `check` routes
the board it builds and writes the copper into it; the next build into the same
`--out` directory read that copper back as a human's hand routing, preserved it, and
routed the design again on top. Copper accumulated on every repeat.

That is a defect in a *verification* tool, so the honest follow-up is not "it is
fixed" but **"which numbers in this repository could have come out of it"**. This
note answers that, and it is referenced from the M13.6 section of
[`m13.md`](m13.md).

## What it took to trigger

All three at once:

1. `aipcb check`, or `aipcb build` without `--fresh`, run **more than once**
2. into the **same output directory**, with the previous run's board still in it,
3. on a design the **router puts copper on** — the merge only kept `segment`, `via`
   and `zone` items, so a design with no routed copper and no declared zones was
   never affected.

A run into a fresh or temporary directory could not accumulate anything, and
`check` with no `--out` uses a temporary directory. So the exposure is narrower
than "every measurement ever taken": it is measurements read out of a *reused*
directory.

## What was re-measured, and what it says

`examples/pcie-sata`, checked three times into one directory on the fixed tree
(2026-08-22, KiCad 9.0.8, the same machine as M13 and M13.5):

| run | board SHA-256 | copper | routed | vias | DRC |
|---|---|---|---|---|---|
| 1 | `0e8f21b1…` | 1108.092 mm | 90 / 90 | 42 | 0 errors, 2 warnings |
| 2 | `0e8f21b1…` | 1108.092 mm | 90 / 90 | 42 | 0 errors, 2 warnings |
| 3 | `0e8f21b1…` | 1108.092 mm | 90 / 90 | 42 | 0 errors, 2 warnings |

Byte-identical boards, identical routing, identical violations — against the
pre-fix behaviour, which took the same design from 1108 mm to 1853 mm to 2462 mm
and invented a clearance error on run 2.

**The flagship board's published numbers are the same numbers.** 1108.1 mm, 90/90,
42 vias, 0 errors and 7 warnings is what M13 and M13.5 both recorded and what the
tool produces today. Those measurements were not taken on an accumulated board.

## What this note does not claim

It does not certify every figure in `m8.md` through `m13.md` one at a time. What it
establishes is the *shape* of the exposure and that the corpus figure most often
quoted is unaffected:

* **Superseded where they disagree.** Any copper length, routed count, via count or
  DRC count in an earlier report that disagrees with what the tool produces today
  should be read as the older number, and today's supersedes it. Nothing found so
  far disagrees.
* **The M13.5 §3 pour-gap measurement is the one that was visibly hit.** It is
  described in that section as crashing and then producing a board with three
  tracks on it no router drew; the measurement was retaken and the number it
  reports (0.1505 mm as built) is from a clean run.
* **Simulation results are not exposed through this path.** `aipcb simulate` routes
  into a temporary directory of its own and never reads the board back from
  `--out`; what it keeps there is slices and results. M13.6 added
  `tests/test_idempotence.py::TestSimulateIsAFunctionOfTheSource` to hold that.

## What now stops it coming back

| test | what it holds |
|---|---|
| `test_check_loop.py::…::test_checking_three_times_changes_nothing` | `check` on `usb-port`, three runs, board bytes and every routing and DRC number identical |
| `test_check_loop.py::…::test_checking_the_flagship_board_three_times_changes_nothing` | the same on `pcie-sata`, where the drift was measured (`AIPCB_FULL_CORPUS=1`) |
| `test_idempotence.py::TestBuildIsAFunctionOfTheSource` | `build` three times into one directory: the UUID guard keeps declared zones from doubling |
| `test_idempotence.py::TestExportIsAFunctionOfTheSource` | `export --build-dir` three times: same board, same fabrication package |
| `test_idempotence.py::TestSimulateIsAFunctionOfTheSource` | `simulate --dry-run` three times into one `--out`: same slice, same digest, same Gerbers |
| `test_idempotence.py::TestSyncPlacementIsAFunctionOfItsInputs` | a report run twice writes nothing and says the same thing twice |
| `test_idempotence.py::test_the_sweep_covers_every_command_that_writes_where_it_reads` | a twelfth command cannot join without an entry here |

The last one is the point. The defect class is *nobody ran it twice*, and the way
to keep closing it is to make a new command declare which side of the line it is
on.

# Assembly outputs: BOM & pick-and-place export (M21)

The fab round is blocked on one gap: an assembled order (the pcie-sata QFN-48 will
not be hand-soldered) requires a BOM and a CPL (pick-and-place) file that a real
assembler accepts. **Important correction to this brief's original premise**:
investigation found that `aipcb export` already advertises "a BOM and a placement
file" and `src/aipcb/compile/export.py` contains BOM/CPL-related code. This
milestone is therefore likely *hardening and verifying existing output against a
real assembler's requirements* rather than building from nothing — the first task
below establishes which. Small milestone, deliberately scoped: exactly what an
assembly order needs, nothing more.

> Named M21 because [M20](m20-placement-quality.md) is placement quality, which was
> split out of M19's review and is sequenced after the fab round.

## Session context (fresh session)

Read the current parts-database format docs, `src/aipcb/compile/export.py` (audit
what BOM/CPL output already exists: which columns, which conventions, whether any
test covers it, whether any assembler format was targeted), and
[`docs/reports/m19.md`](../reports/m19.md). Then, before designing: fetch the
**actual current file-format requirements** for JLCPCB and PCBWay assembly (column
names, rotation conventions, units) — these are documented publicly and they
differ; measure, don't assume. The gap analysis — what the existing code produces
vs. what a real assembler accepts — is the ADR's first section and decides how much
of M21a–c below is new work vs. verification. Record findings in the ADR.

## M21a — Procurement fields in the parts model

Extend the part schema (backward compatible — parts without the fields still
validate):

```yaml
parts:
  STM32G031K8:
    # existing fields...
    mpn: STM32G031K8T6          # manufacturer part number
    manufacturer: STMicroelectronics
    supplier_refs:
      lcsc: C432211              # per-supplier part id, extensible map
    assembly: smt                # smt | tht | dnp | none (default smt)
```

- `dnp` (do-not-populate) must flow through: on the BOM as DNP-marked or excluded
  per the fab's convention (the format research decides), and excluded from CPL
- Validation: warn (not error) on parts lacking `mpn` when an assembly export is
  requested — the report lists exactly which parts block a complete order
- Populate the fields for every part the pcie-sata example uses (this is the forcing
  function the component database needed — real procurement data for the flagship)

## M21b — BOM export

- `aipcb export --bom [--format jlcpcb|pcbway|generic]`: grouped by part (one line
  per unique part, designators aggregated), columns per the researched format;
  `generic` is a clean CSV with everything
- Deterministic ordering (canonical designator sort), byte-stable across runs
- Quantities, values, footprint names as the fab expects them

## M21c — CPL export

- `aipcb export --cpl [--format ...]`: designator, x, y, side, rotation per the
  target format
- **Rotation is the trap — treat it as one**: fab rotation conventions differ from
  KiCad's (zero-orientation reference varies per package, JLCPCB famously needs
  corrections). Research the convention, implement the mapping, and add a
  verification aid: render a placement overlay (top/bottom images with designators
  at their CPL positions/rotations, using the existing render machinery) into
  `out/assembly/` so a human can eyeball polarity and orientation before ordering —
  this catches the classic backwards-diode error the file alone cannot
- `side: back` parts: the M9 limitation (validated, not implemented, places front)
  means back-side CPL is untested — state honestly in the docs that two-sided
  assembly export is unverified until that lands; error if a design actually
  declares back-side parts and an assembly export is requested
- Coordinates in the fab's expected origin/units (the research decides; document the
  transform)

## M21d — The assembly bundle

- `aipcb export --assembly` produces the complete order package: gerbers + drill
  (existing), BOM, CPL, and the placement overlays, zipped per fab convention where
  one exists
- The pcie-sata example's bundle is the acceptance artifact: generated, committed
  under the example's expected-outputs (or hashed in a golden), and *manually
  reviewed once* — the report includes the reviewer's checklist result (all parts
  have mpn, rotations eyeballed against the overlay, DNP handling correct)

## Acceptance

- Format research recorded in an ADR with sources
- pcie-sata: complete assembly bundle generates with zero missing-mpn warnings;
  overlay renders; byte-stable
- All existing examples still export (parts without procurement fields → generic BOM
  works, fab-specific warns with the exact missing list)
- DNP flows correctly through both files; back-side guard errors as specified
- Suite green, mypy strict, schema backward compatible, committed and pushed

## Out of scope

- Supplier API integration (stock/price lookup) — roadmap
- Panelization — still fab-level, still out
- Two-sided assembly (gated on `side: back` implementation)
- Any router/placement work

## Guardrails

- Determinism and backward compatibility as always; export of existing formats
  byte-identical
- The rotation mapping must be verified against the fab's documented convention, not
  inferred from one example — and the overlay exists because files lie and pictures
  don't
- When in doubt, stop and report

## Delivery report (required, in-repo)

`docs/reports/m21.md`: the format research findings, the rotation-convention mapping
with its sources, the pcie-sata bundle checklist result, which parts needed
procurement data added, and any gaps that would block a real order. Measured claims
over asserted ones. Commit and push.

With this milestone green, the fab round has no tooling blockers left: next session
is the design review (schematic vs. the controller's reference design, fab stackup
numbers into the CPWG model) and the order itself.

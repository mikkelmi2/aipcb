# Working notes for this repository

## ADR premises about external tools have expiry dates

**Re-measure before building on them.** Twice now, a decision recorded in an ADR
rested on a claim about an external tool's capabilities that was true when written
and false by the time it mattered:

- **kiutils** — the reason for writing our own s-expression layer.
- **Headless zone fill** — [ADR 0001](docs/decisions/0001-kicad-io.md) excluded
  `pcbnew` partly because it "cannot run headless cleanly". On KiCad 9.0.8 it can,
  measured with no display; see [ADR 0009](docs/decisions/0009-pours.md) Finding 2.
  The same measurement found the opposite direction of staleness too — `kicad-cli`
  still has no fill command, so the CLI could not be assumed to have caught up
  either.

Staleness runs both ways: a limitation may have been lifted, and a capability may
never have arrived. So the rule is not "assume tools improve", it is **measure the
tool you actually have, at the version you actually have, before designing around
it** — and write the measurement down with the version attached, so the next
person can tell how old the claim is. ADR 0009 Finding 1 is written that way on
purpose, and its decision section says explicitly to re-measure at each KiCad major.

This is why milestone prompts in `docs/milestones/` open with a "Before writing
code" section that demands empirical verification: it is not ceremony, it is the
part that has twice saved the project from building on a false premise.

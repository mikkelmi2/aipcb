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

## Budgets re-anchor after every landed candidate

**Each candidate is measured against HEAD at its own start, never against the
profile that ranked the chain.** M19 is where this was learned, and it cost two
candidates:

- M19b's budget — 25 % further, on top of M19a — was priced off `_repair` at 16.30 s
  of a 29.8 s profiled route, of which `_via_sites` was 12.35 s. M19a then took
  `_via_sites` to 5.22 s **before M19b ran**. Most of the second candidate's prize
  had already been collected by the first, and the budget never noticed because both
  were quoted against the same opening profile.
- M19a's own budget — 10 % of corpus `router_seconds` — was a percentage of a corpus
  that grew by a board between the drafting and the build. M19s landed first by the
  milestone's own ordering and now accounts for 82 % of the corpus, so a candidate
  that improved eleven boards by 8.3 % scored 2.9 %.

So, when drafting a chain of candidates:

1. **Re-take the profile at each candidate's own start** and re-price what is left,
   in the delivery report, before building it. A budget inherited from the chain's
   opening profile is a budget measured against a machine that no longer exists.
2. **Where a budget targets a specific cost, quote it in absolute seconds on named
   functions** — "`_via_sites` below 6 s on `pcie-sata`" — rather than as a
   percentage of a moving corpus. Percentages of a corpus are fine for the
   *headline*, and they are the wrong unit for a *target*, because the denominator
   is a thing the same milestone is allowed to change.
3. If the corpus changes mid-chain, **say which denominator each figure uses** and
   report per board as well as per corpus. M19's per-board column is what caught
   both misses; the corpus percentage alone would have hidden which.

The M19 report's [§2.3](docs/reports/m19.md) and [§3.3](docs/reports/m19.md) are the
worked example, and the survey note's
[§6](docs/notes/routing-literature.md#6-measured-results-as-the-closure-rule-requires)
carries the numbers.

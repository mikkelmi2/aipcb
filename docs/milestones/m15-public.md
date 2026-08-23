# M15 — Going public

Make the repository ready to be public and flip the switch. One repo, everything in it — the
engineering history (reports, ADRs, milestone specs) is a differentiator, not laundry; it stays,
organized so a visitor understands what's user documentation and what's history. The README is
short, visual, and honest. This milestone has a hard ordering: the hygiene sweep is first because
it is the only irreversible risk — a secret in history cannot be recalled after the first public
push.

## Session context (fresh session)

Read `docs/reports/m14.md` and the chain reports enough to know the story being told. The
pcie-sata example is the flagship narrative; its rendered A3 schematic (M14) and routed board are
the front-page material.

## M15a — Hygiene sweep (gate: nothing else happens until this is clean)

- Scan the **entire git history**, not just HEAD: gitleaks (or equivalent) for secrets/keys/tokens;
  a manual grep pass for absolute paths with usernames, internal hostnames, email addresses that
  shouldn't be public, and machine-specific details in reports/ADRs (the reports mention hardware —
  that's fine; personal identifiers are not)
- If anything sensitive is found in history: STOP and present the finding with options (history
  rewrite before publication vs. acceptable-to-publish) — do not decide unilaterally, and do not
  push anything anywhere until resolved
- Repo cleanliness: no out/ artifacts, caches, venvs, or local config committed; .gitignore
  complete; large binary files audited (rendered PDFs/images that belong stay, accidental junk goes)
- Third-party content audit: KiCad library symbols/footprints used in examples (license-compatible,
  note attribution), any vendored code (the S-expression handling — check its provenance),
  datasheet-derived content in docs (none should be verbatim), tool references (openEMS, gerber2ems,
  Freerouting are invoked, not embedded — no license contamination, but say so in NOTICE)
- Record the sweep's findings and verdicts in the delivery report

## M15b — License package (decision made: Apache-2.0 with DCO)

- `LICENSE`: Apache-2.0. `NOTICE`: project name, copyright line, attribution for anything vendored
- SPDX headers (`# SPDX-License-Identifier: Apache-2.0`) in source files — script it, don't
  hand-edit 87 files
- DCO: sign-off requirement documented in CONTRIBUTING.md with the exact `git commit -s`
  instruction; enable a DCO check in CI
- Dependency license check in CI (all runtime deps are BSD/MIT-family today — the check exists to
  catch future drift)

## M15c — The README (short, visual, honest)

Rewrite the README as the shop window. Hard constraints:

- **Short.** A visitor should reach the first picture within one screen. Long-form explanation lives
  in docs/, linked — the README links, it does not lecture
- **The visual pipeline is the spine**: YAML source snippet → rendered schematic (the pcie-sata A3)
  → placed board → routed board (the 11 coupled pairs visible) → an SI simulation plot → Gerber/3D
  render. Six images telling the story source-to-fab. Generate the images from the actual examples
  as part of this milestone (kicad-cli renders boards; schematic PDFs exist from M14; matplotlib the
  S-parameter plot from out/si data) and commit them under docs/images/ with the script that
  regenerates them
- **"You do the layout if you want" is prominent, not buried**: a top-level section (early, short)
  stating the three modes — full auto, hybrid (declare critical nets `routing: manual`, route them
  yourself in KiCad; preserve keeps your work), fully manual with aipcb as schematic/placement/
  verification tool — plus the Freerouting bridge in one sentence. This addresses the reflex
  objection ("I don't trust an autorouter") before it forms
- **Honesty section**: what it does well (the demonstrated class — the pcie-sata story in three
  sentences with numbers), what it doesn't do (dense BGA/DDR, HDI, 6+ layers), and that simulation
  validates layout, not fab reality
- Quickstart: install → one small example → open result in KiCad, under 5 minutes, tested by
  actually running it in a clean environment
- One line on the engineering history with a link: "built milestone by milestone by AI agents; the
  specs, reports and decisions are all in docs/ — start with docs/reports/"

## M15d — Contributor surface

- `CONTRIBUTING.md`: where help is wanted (component definitions, examples, docs, testing on real
  boards — not the router core initially), the DCO instruction, the culture in three lines (measured
  claims; honest stops; determinism is sacred), and a pointer to CLAUDE.md explained for humans
- `docs/README.md`: the map — what's user docs (format, workflows, external-routers), what's
  engineering history (reports, decisions, milestones — historical specs, not descriptions of
  current functionality; the existing guard line does this, verify it reads well for a newcomer)
- Issue templates: bug report (asking for the design source + versions) and "board class request"
  (what board did you try, where did it hand over)
- CI public-ready: GitHub Actions running ruff + mypy + the test suite. Resolve the KiCad dependency
  honestly: run the KiCad-dependent tests in a container with KiCad 9.0.8 pinned, or split the suite
  into no-KiCad and KiCad-required jobs — measure what the public runner can do, pick, and document
  the choice in an ADR. CI must be green on the public repo before announcing anything
- Verify the repo name is available where it matters (GitHub org/name, PyPI if the name is to be
  claimed — claim the PyPI name with a placeholder release only if trivially doable, otherwise note
  it). If the name is taken: STOP and present options

## M15e — Flip the switch (quiet launch)

- Final pass: fresh clone → quickstart works → CI green → README renders correctly with images
- Make the repository public. No announcement in this milestone — the quiet-first strategy: let it
  breathe, fix what early eyes find. The announcement (Show HN, r/PrintedCircuitBoard) is
  deliberately a later decision, ideally after the physical pcie-sata board exists
- Tag a release (v0.1.0) with a short changelog distilled from the milestone reports

## Out of scope

- The announcement/launch post (later, with the fab story)
- PyPI publishing beyond name-claiming (later)
- Any code or feature work — if the quickstart test reveals a bug, fix it only if trivial; otherwise
  it's the first public issue, which is honest

## Guardrails

- M15a is a hard gate; sensitive findings stop everything
- Nothing is pushed to any public location until M15a's verdict and your explicit go in the session
  — the agent prepares everything and asks before the flip
- No history rewriting without presenting the finding first
- When in doubt, stop and report

## Delivery report (required, in-repo)

`docs/reports/m15.md`: the hygiene findings and verdicts, the license package contents, the README
before/after (link the images), the CI resolution for the KiCad dependency with measured runner
constraints, the name-availability result, and the final pre-flip checklist with each item's status.
Measured claims over asserted ones. Commit and push — to the now-public repo.

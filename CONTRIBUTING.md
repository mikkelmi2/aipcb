# Contributing to aipcb

Thanks for looking. This project is young and public on purpose: the parts that
work are measured, the parts that don't are written down, and the fastest way to
improve it is for someone to try it on a board we haven't thought of.

## Where help is most wanted

**Component definitions.** The bundled library in `examples/library/` covers what
the examples needed and no more. Adding a part is a small, self-contained,
genuinely useful contribution: a KiCad symbol and footprint reference, a pinout
with electrical types, and the limits worth checking. See
[`docs/format.md`](docs/format.md) for the shape of an entry.

**Examples.** Boards that are real, small enough to read, and different from the
eleven already here. An example that *fails* honestly — that reaches the
hand-over path and says why — is as valuable as one that finishes.

**Documentation.** Especially anywhere the docs assume you already know what a
design file looks like. Newcomer confusion is a bug report about the docs.

**Testing on real boards.** The most useful thing anyone can tell us is what
happened when they sent a generated board to a fab, or opened one in KiCad and
tried to work with it. Simulation validates a layout; it does not validate
reality, and nobody here has closed that loop yet.

**Not the router core, initially.** `src/aipcb/route/` is the part with the most
invariants and the least documentation of them, and a change there can be
silently wrong in a way tests don't catch — M13 found exactly that. Please open
an issue and talk it through before writing code in it. This is not a closed
door, it is a request for a conversation first.

## Sign-off (DCO)

Every commit must carry a `Signed-off-by` line. It certifies that you wrote the
change or otherwise have the right to submit it under the project's licence —
the full text is the [Developer Certificate of Origin
1.1](https://developercertificate.org/).

Git adds the line for you:

```
git commit -s -m "your message"
```

To fix commits that are missing it:

```
git rebase --signoff origin/master     # all commits since master
git commit --amend -s --no-edit        # just the last one
```

CI checks this on every pull request and will tell you exactly which commits are
missing the line.

## Working on the code

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                  # 427 run, 847 skip without KiCad, ~17 s
ruff check . && mypy
python tools/add_spdx.py                # new files need the licence header
```

**Python 3.12 or newer.** Not a style preference: the router is measurably wrong
on 3.11, because CPython 3.12 changed `sum()` of floats and the router costs paths
by summing them. [ADR 0013](docs/decisions/0013-ci.md) Finding 4 has the
bisection.

Tests that need KiCad's libraries, `kicad-cli`, `pcbnew` or a container runtime
skip with a reason rather than failing, so the suite is useful without them —
but two thirds of it is those tests, so a green local run proves less than it
looks. CI runs the whole thing inside `kicad/kicad:9.0.8`, where they all
execute, and that job is what gates a merge — see
[ADR 0013](docs/decisions/0013-ci.md).

## Three things about how this project works

**Measured claims, not asserted ones.** If a change is faster, safer or more
accurate, the pull request says by how much and how that was measured. "Should
be faster" is not a result. This applies to claims about *other* tools too: see
[`CLAUDE.md`](CLAUDE.md) for the two times a decision here rested on a fact about
an external tool that had quietly stopped being true.

**Honest stops.** A tool that cannot finish should say so precisely and hand over
cleanly. Silently producing something wrong is the worst outcome available, and
several of this project's rules exist because it happened once.

**Determinism is sacred.** The same source must compile to byte-identical output,
on any machine, in any order. A change that makes output depend on dict
iteration, wall-clock time, filesystem order or a random seed is a bug even if
every test passes.

## About `CLAUDE.md`

[`CLAUDE.md`](CLAUDE.md) is instructions for AI coding agents working in this
repository, and it is checked in deliberately rather than hidden — most of this
codebase was written by one. It is worth reading even if you are human: it is a
short, blunt statement of the rule that has saved this project twice, which is to
re-measure an external tool's behaviour before designing around a claim about it.
You do not need to follow it to contribute. You do need to follow the three
things above.

## Pull requests

Small and focused beats large and complete. Say what you measured. If you hit
something that looks wrong but you're not sure, open an issue instead of guessing
— "I don't understand what this does" is useful information about the project.

# 0001 — KiCad file I/O: write our own S-expression layer

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M0 (first actions), affects M2, M3, M4 and especially M6

## Context

`aipcb` compiles a semantic source format into `.kicad_sch` and `.kicad_pcb`, and
from M6 onward must *re-read* a board that a human has edited in KiCad and preserve
their work. We need a library that can both emit KiCad 8/9 S-expressions and read
existing ones without losing information.

The brief named [`kiutils`](https://github.com/mvnmgrx/kiutils) as the first
candidate to evaluate. It was evaluated empirically against the KiCad 9.0.8 files
shipped on this machine, not just read about.

## Options

1. **`kiutils`** — a typed dataclass model of the KiCad format.
2. **A generic, lossless S-expression tree** written here, with typed emitters
   layered on top only for the constructs `aipcb` actually generates.
3. **KiCad's Python API (`pcbnew`)** — excluded up front by the brief: it forces a
   KiCad runtime into CI and cannot run headless cleanly.

## Evidence

`kiutils` 1.4.8 (the current release) *parses* KiCad 9 files, but round-tripping
them is lossy. Reading `demos/pic_programmer` and writing it straight back:

| File | Original | After round-trip |
|---|---|---|
| `.kicad_pcb` | 698,532 bytes | 598,906 bytes (−14.3%) |
| `.kicad_sch` | 196,686 bytes | 170,651 bytes (−13.2%) |

Counting tokens before and after shows what disappears:

| Token | In file | After round-trip | Consequence |
|---|---|---|---|
| `uuid` (pcb) | 2072 | **0** | replaced by 370 KiCad-6 `tstamp` tokens |
| `sheetname` / `sheetfile` | 56 / 56 | 0 / 0 | footprint→sheet association destroyed |
| `hide` | 128 (pcb), 391 (sch) | 0 | every hidden field becomes visible |
| `unlocked` | 126 | 0 | field placement flags lost |
| `dnp`, `exclude_from_sim` | 106, 135 | 105, 0 | BOM/simulation intent lost |
| `embedded_fonts`, `generator_version` | 64, 1 | 0 | KiCad 9 required tokens absent |

It also *invents* tokens KiCad 9 does not use (`tstamp`, `tedit`, `plotreference`,
`viasonmask`), because its model targets the KiCad 6 format — its constants still
declare `version 20211014` while KiCad 9 writes `20241229` (pcb) and `20250114`
(sch), and its docstrings cite the v6 file-format spec.

The `uuid` row is the decisive one. `kicad-cli`'s JSON reports identify every
violation by the `uuid` of the offending item:

```json
{"description": "Symbol J1 [DB9]", "pos": {"x": 0.3175, "y": 0.9144},
 "uuid": "00000000-0000-0000-0000-0000442a4c93"}
```

That UUID is byte-for-byte the `(uuid …)` token in the source file — verified by
grepping the demo schematic. Deterministic UUIDs derived from source paths are
therefore the spine of this project: they are how M4 maps a violation back to the
YAML element that owns it, and how M6 decides which hand-edited tracks survive a
rebuild. A library that erases all 2072 of them cannot be used.

## Decision

**Write our own S-expression layer** (`aipcb.kicad.sexpr`), and build typed
emitters on top of it for the constructs we generate.

The key insight is that reading and writing have different requirements, and
`kiutils` fails because it forces one schema to serve both:

* **Writing** — we only ever emit the subset of KiCad we generate ourselves. That
  subset is small, fully under our control, and needs a deterministic formatter.
* **Reading** (M6) — we must handle *arbitrary* KiCad files, including every
  construct we know nothing about. A schema-mapped model necessarily drops what it
  does not model. A generic tree drops nothing, because it never interprets
  anything: we navigate to the nodes we own, edit those, and leave the rest
  untouched.

So `sexpr.py` parses into a generic `SNode`/`Atom` tree. Atoms keep their original
lexical form — `1.6` stays the string `"1.6"` and is never reparsed as a float —
which is what makes byte-stability achievable without reimplementing KiCad's
number formatting.

## Validation

The losslessness claim is tested, not asserted. Every KiCad file shipped with the
distribution — demos, all 224 symbol libraries, all 155 footprint libraries,
templates: **16,186 files, 526 MB** — is parsed, re-emitted, and re-parsed, with
the two trees compared for equality.

**16,183 / 16,186 round-trip losslessly.** The three exceptions are not parser
defects:

* `demos/vme-wren/vme-wren.kicad_dru` — `.kicad_dru` legitimately holds *several*
  top-level expressions. Handled by `parse_all()` / `dump_all()`; it round-trips
  losslessly through those.
* Two files under `demos/royalblue54L_feather/` — genuinely malformed. An
  independent paren counter ends at depth −7 with 16 top-level roots, i.e. they
  contain more closing parens than opening ones. Rejecting them with a
  line/column error is the correct behaviour.

This corpus test lives in `tests/test_sexpr_corpus.py` and skips with a clear
message when KiCad's libraries are not installed.

## Consequences

* **We own the format surface.** Supporting a new KiCad version means adding the
  tokens we emit, not waiting for an upstream library to catch up.
* **No dependency on `kiutils`.** One fewer unmaintained-library risk; the
  `pyproject.toml` dependency list stays at pydantic + typer + pyyaml.
* **We must track KiCad's format ourselves.** Mitigated by the corpus test above
  and by the fact that `kicad-cli` validates every file we generate.
* **Formatting is ours, not KiCad's.** We do not reproduce KiCad's exact
  pretty-printing. Our output is stable given the same input, which is what the
  git-friendliness requirement asks for; when a human saves the file in KiCad it
  gets reformatted to KiCad's taste, and reading it back is unaffected because the
  parser is whitespace-insensitive.

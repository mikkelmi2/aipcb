# 0002 — Source format: YAML with a declarative module system

* **Status:** Accepted
* **Date:** 2026-08-20
* **Context:** milestone M1

## Context

The brief leaves the layer-1 syntax open: "`design.yaml` or a small DSL — you
propose, justify the choice". The primary author and reader of this format is an
AI agent in an edit→compile→check loop; a human reads it occasionally and reviews
its diffs.

## Options

1. **YAML + pydantic models.**
2. **A purpose-built DSL** with its own grammar and parser.
3. **Python as the DSL** (the SKiDL approach): the source *is* a program.

## Decision

**YAML, validated by pydantic**, with parameterised modules expressed as
declarative templates rather than as functions in a general-purpose language.

## Rationale

The deciding factor is who writes this format. An LLM emits syntactically valid
YAML with no prompting, no grammar in context, and no examples — it is one of the
few structured formats that is genuinely free at the point of use. A bespoke DSL
would have to earn its keep against that, and it cannot: every token of grammar
the agent must be taught is a token not spent on the actual design, and every
novel syntax is a new class of malformed output to recover from.

YAML also gives us, for free, the things the check-loop depends on:

* **Line and column numbers.** Every diagnostic can point at the exact element
  that caused it. We attach source positions during loading (`aipcb.source`), so a
  DRC violation on net `USB_DP` resolves to `usb_port.yaml:31:3`. With a hand-rolled
  parser this is work; with Python-as-DSL it is impossible in general, because the
  offending net may have been produced by a loop three call-frames deep.
* **Partial reads (M5).** `aipcb query` can extract one module with its neighbours
  because the file is data at rest. A program must be *executed* to know what it
  contains, which makes token-economical partial reads unattainable.
* **Round-tripping and machine edits.** An agent can rewrite one field without
  regenerating the file.
* **Reviewable diffs.** Sorted keys and block style keep `git diff` semantic.

Option 3 fails hardest on the project's central premise. If the source is a
program, the source of truth is its *output*, and the design cannot be analysed,
queried, or diffed without running it.

## The cost, and how we pay it

YAML is not a programming language, so `buck_converter(vin, vout, iout_max)` cannot
be a function call. We get parameterisation from a template/instantiation model:
a `modules:` block defines a module with typed `params` and named `ports`, and an
`instances:` block stamps it out with argument values and port connections.

```yaml
modules:
  decoupled_rail:
    params:
      count: { type: int, default: 2 }
      cap:   { type: part, default: C_100n_0402 }
    ports: [VIN, GND]
    components:
      C:  { part: "{{ cap }}", count: "{{ count }}", role: decoupling,
            pins: { "1": VIN, "2": GND } }
```

This is deliberately *not* Turing-complete. Substitution, repetition and
conditional inclusion cover the parameterisation real designs need, and stopping
there is what keeps the format statically analysable — which is the whole reason
we chose data over code. If a design ever genuinely needs computation, the answer
is a generator that emits `design.yaml`, keeping the checked-in source declarative.

## Consequences

* YAML's sharp edges are real and are blunted at load time: we use a strict loader
  where `on`/`off`/`yes`/`no` stay strings (the Norway problem — a net named `NO`
  must not become `False`), duplicate keys are an error rather than a silent
  overwrite, and tabs in indentation produce a readable message.
* pydantic models are the single schema definition; a JSON Schema is generated
  from them (`aipcb schema`) for editor completion, rather than maintained twice.
* Module expansion happens before validation of the elaborated netlist, so
  diagnostics must carry both the instance location and the module-definition
  location. `aipcb.source.Loc` therefore supports a provenance chain.

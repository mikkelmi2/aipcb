# Routing with an external router

aipcb does not integrate an external router, and this document is not a step towards
integrating one. [ADR 0006](decisions/0006-routing-approach.md) rejected that, and
the reasoning stands: the topological router is what this project is *for*, and
wiring somebody else's engine into the pipeline would mean owning its output without
owning its behaviour.

What M14e adds is the thing that was missing: **a documented bridge**, which is a
different object.

```
aipcb                    you / your agent               aipcb
─────                    ───────────────                ─────
export --dsn      ──▶    freerouting -de -do     ──▶    import --ses
(existing copper                                        (splice, verify,
 marked unmovable)                                       check, report)
```

aipcb writes one file and reads another. It never launches the router, never parses
its logs, and promises nothing about its output beyond what `aipcb check` can
measure of the copper that comes back.

---

## The recipe

Three aipcb commands and one invocation of somebody else's jar. All of it headless;
no GUI is opened at any point.

```bash
# 1. Declare which nets are not aipcb's, and route the rest.
#    (routing: manual on a class or a net -- see docs/workflows.md)
aipcb route all design.yaml

# 2. Write the DSN. Everything already routed is fixed in the file.
aipcb export design.yaml --dsn
#    wrote design/mcu-4layer.dsn (27460 bytes, 85 pieces of copper fixed)
#      to route: PB0, PB1, PB2, RESET

# 3. Route it with whatever you like. Freerouting's own CLI, no display needed:
freerouting -de mcu-4layer.dsn -do mcu-4layer.ses

# 4. Bring it back. Imports, splices, verifies against the source, then checks.
aipcb import design.yaml --ses mcu-4layer.ses
#    imported 25 pieces of copper into mcu-4layer.kicad_pcb
#      manual-routed: 4
#      auto-routed: 7
#      drc: ran (0 errors)
```

Both aipcb commands take `--json`. Step 4's payload carries `import.splice.taken`
(copper added, per net), `import.drift` (where the session disagrees with the source),
`import.still_pending` (declared-manual nets the router did not route) and the full
four-state net table.

`--board` overrides which board either command uses; both default to the one beside
the design file.

---

## What this environment measured

Every claim below was measured on **KiCad 9.0.8** and **Freerouting 2.3.0** on
2026-08-22, before any of the bridge was written. The dates matter — see
[CLAUDE.md](../CLAUDE.md) on ADR premises having expiry dates.

| Question | Answer |
|---|---|
| Does `kicad-cli` export DSN or import SES? | **No.** `kicad-cli pcb` offers `drc`, `export` and `render` only, and `export` has no Specctra format. |
| Does `pcbnew` do it headlessly? | **Yes.** `ExportSpecctraDSN` and `ImportSpecctraSES`, both with `DISPLAY` unset. |
| Does Freerouting have a working headless CLI? | **Yes.** `freerouting -de in.dsn -do out.ses` completed with no display, warning `Couldn't get screen resolution` and proceeding. |

So the bridge runs through the `pcbnew` subprocess that
[ADR 0009](decisions/0009-pours.md) already sanctioned for zone filling — the same
boundary, the same version lock, the same "no `pcbnew` in aipcb's own process" rule.

If your machine has no interpreter that can import `pcbnew`, both commands fail with
that as the message rather than with a traceback. Set `AIPCB_PCBNEW_PYTHON` to one
that does.

---

## The contract

Read this part. It is short, and the whole bridge rests on it.

**External copper is manual copper.** Imported tracks and vias have UUIDs aipcb has
never seen, which under [ADR 0005](decisions/0005-incremental-builds.md) makes them
somebody else's work: preserved by every later `aipcb build`, treated as fixed
obstacles by every later `aipcb route all`, checked by `aipcb check` exactly as
aipcb's own copper is checked.

**There is no source mapping.** aipcb's own routes trace back to a topology in the
source and to the lines of YAML that produced them. An imported track traces back to
nothing. `aipcb check` will tell you it is illegal; it cannot tell you which decision
made it so.

**There is no determinism.** Two Freerouting runs on the same DSN produce different
boards. That is expected, and it is why external copper is exempt from the
byte-stability bar that everything aipcb produces is held to. If you need a
reproducible board, keep the SES file — that, not the run that made it, is the
artefact.

**Existing copper cannot be destroyed, in either direction.** Two separate
mechanisms, because there are two separate ways to lose it:

* *On the way out*, every wire and via already on the board is rewritten from
  Specctra's `(type route)` to `(type fix)`. KiCad exports copper as `route`, which
  tells the external router it may rip all of it up.
* *On the way back*, only the copper for nets that were actually pending is taken out
  of the import. This is not belt-and-braces: `pcbnew.ImportSpecctraSES` does not
  *add* a session's routing to a board, it **replaces** the board's routing with it.
  Measured on `examples/mcu-4layer`, importing a session that routed four ISP signals
  removed 97 tracks and 52 stitching vias, and nothing said so — the file parsed, the
  import returned success, and DRC found no errors, because copper that is gone
  violates no rule.

  If the session carries copper for a net the DSN fixed, it is counted, ignored, and
  reported as `session-touched-fixed-nets`.

**Geometry is verified, not trusted.** SES import reconstructs net classes from
names, and reconstruction can be wrong. Every track width and via size that comes
back is compared against the class the *source* declares, and any difference is
reported as `session-geometry-drift` — with the expected and found values. It is
never corrected: silently widening somebody else's track would make this bridge a
router, which is the thing ADR 0006 declined to be.

---

## The rule with teeth

> **Never send a controlled-impedance class to an external router.**

A class with `impedance_diff_ohm` carries a width derived from the stackup, a gap
that is an input to that derivation, a coupling budget, a maximum skew and a named
reference plane. An external router knows about none of them. It will return two
traces that connect the right pads and are neither coupled, nor 85 Ω, nor
length-matched — and aipcb cannot check what it did not decide, because half of M11's
verification is about *how* the pair was built rather than where it ended up.

`aipcb export --dsn` warns when a declared-manual pending net is on such a class:

```
warning[controlled-impedance-to-external-router]: PCIE_TXP, PCIE_TXN are on a
controlled-impedance net class and about to be handed to an external router
  hint: an external router knows nothing about coupling, skew, the reference plane
  or the derived pair geometry, and aipcb cannot check what it did not decide.
  Route these with `aipcb route all` or by hand, and send the rest.
```

It is a warning rather than a refusal — somebody may have a reason, and this program
does not know every board. But it is never quiet about it.

---

## Pitfalls

**The router may not finish, and will say so in its own words.** Freerouting reports
its own unrouted count and its own violation count, against its own rule model.
Neither is aipcb's verdict. `aipcb import` reports which declared-manual nets still
have no copper (`still_pending`), and `aipcb check` reports what KiCad's DRC makes of
what did arrive. Those two are the verdict.

**Fixed copper makes the board harder to route, not easier.** On a board with planes,
stitching vias and finished pairs already in place, an external router is working in
what is left. Measured on `examples/mcu-4layer`: with 85 pieces of copper fixed,
Freerouting reported 19 violations against its own model, and the four nets it was
asked for still came back routable and DRC-clean after the splice. On
`examples/routing-demo` — a board that exists to be congested — it reported 2 unrouted
and did not finish. Both are honest outcomes.

**Planes are not signal layers, and the DSN does not say so as loudly as aipcb does.**
`layer_forbid` and `prefer_layers` are aipcb's; a DSN carries layer types, and an
external router's interpretation of them is its own. Check what came back.

**Net names round-trip; net *codes* do not.** The importer renumbers, so anything
matching copper between the two boards has to do it by name. aipcb does; if you write
your own tooling against these files, do the same.

---

## Explicitly not built

Stated here so nobody has to infer it from an absent flag:

* **No `--engine` flag.** There is no router selection, because aipcb selects no
  router.
* **No invocation.** aipcb will not launch Freerouting, or anything else. Step 3 is
  yours, and it is visible in your shell history because that is the point.
* **No log parsing.** Freerouting's scores, passes and violation counts are its own
  report to you. aipcb does not read them and does not relay them.
* **No promise about the output** beyond `aipcb check`'s verdict on the copper. The
  bridge provides pipes and judgement. It does not provide endorsement.

---

## See also

* [`docs/workflows.md`](workflows.md) — the three routing modes and the
  human-in-KiCad loop.
* [ADR 0006](decisions/0006-routing-approach.md) — why aipcb routes the way it does,
  and the M14e amendment that places this bridge.
* [ADR 0009](decisions/0009-pours.md) — the `pcbnew`-subprocess boundary this reuses.

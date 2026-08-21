# What the router is trying to minimise

Every choice the multilayer router makes comes out of one number: the cost of a
path. This document lists the terms that number is made of, what each one defaults
to, and why.

All of them are in **millimetres of track**, because that is the only unit the
search actually has. "A via costs 5" means "a via is worth taking if it saves more
than 5 mm of copper". Reading them that way is what makes them arguable rather than
magic.

They live in [`aipcb/route/costs.py`](../src/aipcb/route/costs.py) as one frozen
dataclass, `CostModel`, so a board that wants different numbers overrides fields
rather than editing the search.

## The cost of a move

```
cost = length
     + via_cost × n_vias                 # a layer change
     + layer_penalty(layer, net class)   # ∞ for a plane
     + congestion(cut)                   # → very large as a cut fills up
     + direction_penalty                 # soft H/V preference from the stackup
```

The search is an A\* over one graph spanning every signal layer, joined at candidate
via sites; the details are in [ADR 0007](decisions/0007-multilayer.md) and the code
is [`aipcb/route/graph.py`](../src/aipcb/route/graph.py).

## The parameters

| Parameter | Default | What it means |
|---|---|---|
| `via_cost_mm` | 5.0 | What one via is worth in millimetres of track. |
| `non_preferred_layer_mm` | 8.0 | Cost of using a layer the net class did not ask for. |
| `plane_layer_mm` | ∞ | Cost of routing on a layer the stackup gave over to a plane. |
| `direction_penalty_mm_per_mm` | 0.25 | Extra cost per millimetre travelled against a layer's grain. |
| `congestion_present` | 0.8 | How steeply the *first* pass charges for sharing a corridor. |
| `congestion_growth` | 1.9 | How much that rises each negotiation pass. |
| `congestion_history` | 4.0 mm | Added to a cut's price for each pass it stays over-subscribed. |
| `congestion_cap` | 1e6 | The price of a cut that is already full. |
| `rip_up_protected` | ×12 | How much harder a `rip_up: protected` net is to move. |
| `rip_up_never` | ×10⁴ | The same for `rip_up: never`. |
| `iterations` | 12 | Negotiation passes before giving up. |

### `via_cost_mm` — 5.0

A via is a drilled hole, an impedance discontinuity, a stub, and a blocked spot on
every layer it passes through. The brief's range is 3–10 mm. Five sits where a via
is worth taking to escape a fine-pitch part or to cross a busy corridor, and not to
shave a corner off a short run — which is exactly the behaviour the bundled examples
show: `ldo-supply` takes none, `led-blinker` takes one to escape the DIP, and
`usb-port` takes four to get out of the Micro-B's fan-out.

### `non_preferred_layer_mm` — 8.0

Large enough that a class saying `prefer_layers: [F.Cu]` stays on F.Cu wherever
F.Cu works. Small enough that "stay on F.Cu" never becomes "fail rather than move",
which is what an infinite penalty would mean and is almost never what a designer
wants. A class that really does mean *never* says `layer_forbid`.

### `plane_layer_mm` — infinite

Not tuned: chosen. A plane with a signal cut through it is not a plane, and the
reference it gives every other layer is the reason the stackup has one. A net class
opts in by naming the layer in `prefer_layers`, which takes the layer out of the
plane set *for that class* rather than discounting the penalty — so opting in is a
decision, not an accident of arithmetic.

### `direction_penalty_mm_per_mm` — 0.25

Preferred directions (`stackup.preferred_direction`) make a dense board tractable by
keeping crossings on separate layers. A router that obeys them absolutely produces
staircases where a diagonal would do, so the hint is soft: going the wrong way costs
25% more per millimetre, charged on the component of the move that runs across the
grain. A diagonal therefore pays about half of what a right-angle turn does. Layers
the stackup says nothing about pay nothing.

### The congestion terms

These are PathFinder's, and they are the reason the router converges at all.

`congestion` is charged per cut crossed, as a multiplier on the length of the move
into it:

```
occupancy = (already_used + this_net_demand) / capacity
congestion = step_length × present × occupancy       (occupancy ≤ 1)
           = congestion_cap                          (occupancy > 1)
```

with `history` added on top. Three consequences worth knowing:

* **Narrow corridors cost more even when empty.** A cut that barely fits one track
  has occupancy near 1 from the first net that crosses it; a wide one has occupancy
  near zero. So the search spends open space first and saves tight gaps for the
  routes that have no alternative — which is M7's measured `--congestion` behaviour,
  arriving here as a consequence rather than as a separate term.
* **`present` rises each pass** (`congestion_present × congestion_growth^pass`), so
  early passes let nets find their natural paths and later ones force the argument.
* **`history` is permanent.** It is what resolves the second-order congestion two
  nets fall into when each looks free only because the other has just left. Four
  millimetres per pass is comparable to a via, so a corridor that stays contested
  becomes worth going round or under — but only after `present` has had its chance.

`congestion_cap` is large but finite on purpose: the whole point of negotiation is
that a net may sit on a contested cut *for now* and be persuaded off it later.

### The rip-up multipliers

`rip_up` is a net class field. On each over-subscribed cut the net that is hardest
to rip up keeps its place and the rest re-route, where "hardest" is
`priority × multiplier`:

| `rip_up` | multiplier | effect |
|---|---|---|
| `normal` | ×1 | Moves like anything else of its priority. |
| `protected` | ×12 | Disturbed only when the alternative is failing to route something else — about a dozen ordinary nets' worth of resistance. |
| `never` | ×10⁴ | Ripped only as the last thing tried before declaring the board unroutable. |

`never` is not infinite. Reporting "this board cannot be routed" without having
tried is worse than trying; what matters is that the failure report *names* the
`never` net that was holding the corridor, which it does.

### `iterations` — 12

PathFinder converges on realistic boards in far fewer. The limit exists so that a
genuinely over-constrained board fails in seconds with a report rather than
spinning. The negotiation also stops early when three passes in a row fail to reduce
the number of over-subscribed cuts, because a board that has not improved in three
passes is over-subscribed rather than unlucky. Every pass is logged, and
`aipcb route all --json` reports how many ran and whether they converged.

## Priority, and where it comes from

`priority` is a net class field, 0–100. It decides the order nets are routed in and
who keeps a contested corridor. When a class does not state one it gets a default
from its name:

| class | default priority |
|---|---|
| `diff_pair`, `usb` | 80 |
| `high_speed`, `clock` | 75 |
| `analog` | 65 |
| `power` | 60 |
| `ground` | 55 |
| `signal`, anything else | 50 |
| any differential pair, whatever its class | 80 |

These are M7's measured ordering re-expressed as priorities, so the heuristic and
the source field are one mechanism rather than two.

Within a priority, nets are ordered by **difficulty**: the straight-line length
times the demand, divided by the narrowest cut a route leaving either pad has to get
through. A pair escaping a 0.65 mm-pitch receptacle scores enormously and chooses
first; a ground hop between two capacitors in open board scores near nothing and
goes last.

## Two knobs on the command line

```bash
aipcb route all design.yaml --congestion 1.0 --layers F.Cu,B.Cu
```

`--congestion` scales the whole congestion term: `0` routes purely for length and
via cost, ignoring how full a corridor is. It is worth trying on a board that fails
to converge, to see whether congestion or geometry is the problem. `--layers` limits
the run to named copper layers, which is how you ask "would this board route on one
layer?" — and on `examples/congestion` the answer is no.

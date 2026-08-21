"""What the router is trying to minimise, as named numbers.

Every term the multilayer search weighs lives here, with a default and a reason.
Scattering these through the search would make the router's behaviour a matter of
archaeology; collecting them makes it a matter of reading one file — and of
overriding one field when a board wants something else.

Everything is expressed **in millimetres of track**, which is the only unit the
search actually has. "A via costs 5" means "a via is worth taking if it saves more
than 5 mm of copper". That framing is what makes the numbers arguable rather than
magic, and each one's rationale is in :doc:`../routing-costs`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEFAULT_COSTS", "CostModel"]


@dataclass(frozen=True, slots=True)
class CostModel:
    """The cost of everything the router can choose to do."""

    via_cost_mm: float = 5.0
    """What one via is worth in millimetres of track.

    A via is not free: it is a drilled hole, a stub, an impedance discontinuity and
    a blocked spot on every layer it passes. The brief's range is 3-10 mm; 5 mm sits
    where a via is taken to escape a fine-pitch part or to cross a busy corridor,
    and not to shave a corner off a short run.
    """

    non_preferred_layer_mm: float = 8.0
    """Penalty for routing on a layer the net class did not ask for.

    Large enough that a class stating `prefer_layers: [F.Cu]` stays on F.Cu wherever
    F.Cu works, small enough that "stay on F.Cu" never becomes "fail rather than
    move", which is what an infinite penalty would mean.
    """

    plane_layer_mm: float = float("inf")
    """Penalty for routing on a layer the stackup gave over to a plane.

    Infinite by construction, not by tuning: a plane with a signal cut through it is
    not a plane, and the reference it provides to every other layer is the reason
    the stackup has one. A net class opts in by naming the layer in
    `prefer_layers`, which removes the layer from the plane set for that class
    rather than discounting this penalty.
    """

    direction_penalty_mm_per_mm: float = 0.25
    """Extra cost per millimetre travelled against a layer's preferred direction.

    A soft hint, and deliberately soft: preferred directions make a dense board
    tractable by keeping crossings on separate layers, but a router that obeys them
    absolutely produces staircases where a diagonal would do. A quarter-millimetre
    per millimetre means going the wrong way is 25% more expensive, which biases
    without dictating. Zero for layers the stackup says nothing about.
    """

    congestion_present: float = 0.8
    """How steeply the first iteration charges for sharing a corridor.

    PathFinder's *present* congestion term. Low on the first pass so that nets find
    their natural paths before being pushed around; the schedule below raises it.
    """

    congestion_growth: float = 1.9
    """How much the present factor rises per negotiation iteration.

    McMurchie and Ebeling's original schedule multiplies by about 2 each iteration:
    fast enough to converge in a handful of passes, gentle enough that the first
    over-subscribed corridor does not simply explode. Fixed rather than adaptive, so
    a given source always takes the same path to the same answer.
    """

    congestion_history: float = 4.0
    """Millimetres added to an over-used cut's price, per iteration it stays over-used.

    History is what resolves *second-order* congestion: two nets that keep swapping
    corridors because each looks free once the other has left. Charging the corridor
    itself, permanently, breaks the oscillation. Four millimetres is comparable to a
    via, so a corridor that stays contested for a few passes becomes worth going
    round or under -- but only after the present-congestion term has had its chance.
    """

    congestion_cap: float = 1e6
    """The cost of an edge that is full. Not literally infinite.

    A truly infinite cost would make an over-subscribed edge unusable, and the whole
    point of negotiation is that a net may use a contested edge *for now* and be
    persuaded off it later. Large enough to be a last resort, finite enough to be a
    resort at all.
    """

    rip_up_protected: float = 12.0
    """How much more it costs to rip up a `rip_up: protected` net.

    Expressed as a multiplier on the net's rip-up score, so a protected net is
    disturbed only when the alternative is failing to route something else. Twelve
    puts a protected net behind roughly a dozen ordinary ones.
    """

    rip_up_never: float = 1e4
    """The multiplier for `rip_up: never`.

    Not infinite: a `never` net that genuinely blocks convergence must still be
    ripped up as the very last thing tried, because reporting "this board cannot be
    routed" without having tried is worse than trying. What matters is that the
    failure report names it.
    """

    iterations: int = 12
    """How many negotiation passes before giving up.

    PathFinder converges on realistic boards in far fewer; the limit exists so a
    genuinely over-constrained board fails in seconds with a report rather than
    spinning. Every iteration is logged.
    """


#: The defaults, used when nothing overrides them.
DEFAULT_COSTS = CostModel()

"""The stackup as the router sees it: which layers exist, and what they are for.

`layout.stackup` describes a physical board. This module turns that into routing
law — which layers a net class may use, what it costs to use each, which layers a
via barrel passes through, and how much conductor that barrel adds.

The rule that does the most work here is the plane rule. A layer given over to a
plane is excluded from signal routing outright, because a plane with signals cut
through it stops being the reference the other layers were designed against. A net
class opts back in by naming the layer in `prefer_layers`, which is deliberate
enough to be a decision rather than an accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aipcb.model.layout import Layout, NetClass, Stackup, ViaType, copper_layer_names
from aipcb.route.costs import DEFAULT_COSTS, CostModel

__all__ = ["RoutingStack", "stack_for"]


@dataclass(frozen=True, slots=True)
class RoutingStack:
    """Which copper layers there are, and what each is allowed to carry."""

    copper: tuple[str, ...]
    """Every copper layer, front to back, in KiCad's order."""
    planes: dict[str, str] = field(default_factory=dict)
    """Layer to the net its plane carries."""
    via_types: frozenset[ViaType] = frozenset({"through"})
    direction: dict[str, str] = field(default_factory=dict)
    stackup: Stackup = field(default_factory=Stackup)
    costs: CostModel = DEFAULT_COSTS

    # -- which layers ----------------------------------------------------------

    @property
    def signal(self) -> tuple[str, ...]:
        """Layers open to routing for a net class that says nothing."""
        return tuple(name for name in self.copper if name not in self.planes)

    def layers_for(self, net_class: NetClass) -> tuple[str, ...]:
        """The layers this class may use, in front-to-back order.

        A plane layer is available only to a class that names it explicitly, and
        `layer_forbid` outranks everything: a class that forbids a layer never gets
        it, even by preferring it, which the model already refuses to let you say.
        """
        opted_in = set(net_class.prefer_layers) & set(self.planes)
        forbidden = set(net_class.layer_forbid)
        return tuple(
            name
            for name in self.copper
            if name not in forbidden and (name not in self.planes or name in opted_in)
        )

    def layer_penalty(self, layer: str, net_class: NetClass) -> float:
        """What it costs, in millimetres of track, to route on ``layer``.

        Infinite means "not this layer", and the search will simply not go there.
        """
        if layer not in self.layers_for(net_class):
            return self.costs.plane_layer_mm
        if not net_class.prefer_layers or layer in net_class.prefer_layers:
            return 0.0
        return self.costs.non_preferred_layer_mm

    def direction_penalty(self, layer: str, dx: float, dy: float) -> float:
        """The cost of travelling ``(dx, dy)`` against the layer's grain.

        Charged on the component that runs the wrong way, so a diagonal pays half
        of what a right-angle turn would. A layer with no stated direction pays
        nothing, which is what every board built before this milestone does.
        """
        preference = self.direction.get(layer, "any")
        if preference == "horizontal":
            across = abs(dy)
        elif preference == "vertical":
            across = abs(dx)
        else:
            return 0.0
        return across * self.costs.direction_penalty_mm_per_mm

    # -- vias ------------------------------------------------------------------

    def index(self, layer: str) -> int:
        return self.copper.index(layer)

    def via_type(self, a: str, b: str) -> ViaType | None:
        """The cheapest via type this stackup allows between two layers.

        Blind and buried vias are used only when the stackup lists them, because
        they are a real extra cost at the fabricator and nobody should discover them
        in a quote. Everything else falls back to a through via -- on a four-layer
        board with through vias only, an In1-to-In2 hop still needs a hole through
        the whole board, and that is what it gets.

        ``None`` means there is nothing to build: the two layers are the same, or
        the stackup lists only spans that cannot join them.
        """
        if a == b:
            return None
        outer = {self.copper[0], self.copper[-1]}
        touches_outer = len({a, b} & outer)
        if touches_outer == 2:
            preferred: tuple[ViaType, ...] = ("through",)
        elif touches_outer == 1:
            preferred = ("blind", "through")
        else:
            preferred = ("buried", "through")
        for kind in preferred:
            if kind in self.via_types:
                return kind
        return None

    def barrel_span(self, a: str, b: str) -> tuple[str, ...]:
        """Every copper layer a via between ``a`` and ``b`` physically passes.

        A through via on a four-layer board is an obstacle on all four layers even
        though it carries signal on two: the hole is there regardless. Forgetting
        that is how a router puts an inner-layer track through a drill.
        """
        kind = self.via_type(a, b)
        if kind is None:
            return ()
        if kind == "through":
            return self.copper
        low, high = sorted((self.index(a), self.index(b)))
        return self.copper[low : high + 1]

    def barrel_length(self, a: str, b: str) -> float:
        """How much conductor the barrel adds, for length matching."""
        span = self.barrel_span(a, b)
        if not span:
            return 0.0
        return self.stackup.barrel_length_mm(span[0], span[-1])


def stack_for(layout: Layout | None, costs: CostModel = DEFAULT_COSTS) -> RoutingStack:
    """The routing view of a design's stackup, defaults included."""
    stackup = layout.stackup if layout is not None else Stackup()
    return RoutingStack(
        copper=copper_layer_names(stackup.copper_layers),
        planes=dict(stackup.plane_layers),
        via_types=frozenset(stackup.via_types),
        direction=dict(stackup.preferred_direction),
        stackup=stackup,
        costs=costs,
    )

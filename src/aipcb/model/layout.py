"""The layout-intent layer -- layer 2 of the architecture.

This layer says what the board should be *like*, not where every copper feature
goes: how thick the stack is, which nets are fat, what must sit near what. The
compiler turns intent into geometry; the source never stores geometry that the
compiler could derive.

Everything here is optional. A design that declares no ``layout:`` block still
compiles -- it simply gets defaults.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

__all__ = [
    "COPPER_THICKNESS_MM",
    "MASK_THICKNESS_MM",
    "BoardOutline",
    "Keepout",
    "Layer",
    "Layout",
    "NetClass",
    "Placement",
    "PlacementRule",
    "PlaneLayer",
    "Stackup",
    "StackupLayer",
    "ViaType",
    "copper_layer_names",
]

from aipcb.model.common import Layer, Strict
from aipcb.route.model import RouteTopology


class NetClass(Strict):
    """Routing rules for a class of nets. Maps onto a KiCad net class."""

    trace_width_mm: float = Field(default=0.25, gt=0)
    clearance_mm: float = Field(default=0.2, gt=0)
    via_diameter_mm: float = Field(default=0.6, gt=0)
    via_drill_mm: float = Field(default=0.3, gt=0)
    diff_pair_width_mm: float | None = Field(default=None, gt=0)
    diff_pair_gap_mm: float | None = Field(default=None, gt=0)
    impedance_ohm: float | None = Field(default=None, gt=0)
    max_skew_mm: float | None = Field(
        default=None, ge=0, description="Length-match tolerance within the class.",
    )
    prefer_layers: tuple[Layer, ...] = ()
    layer_forbid: tuple[Layer, ...] = Field(
        default=(),
        description="Layers this class may never use. Outranks `prefer_layers`.",
    )
    priority: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Routing priority, 0-100. Higher routes first and is harder to "
        "rip up. Unset means the default for the class name.",
    )
    rip_up: Literal["never", "protected", "normal"] = Field(
        default="normal",
        description="How readily the negotiating router may rip this class up.",
    )
    description: str | None = None

    @model_validator(mode="after")
    def _preference_is_not_self_contradictory(self) -> NetClass:
        clash = sorted(set(self.prefer_layers) & set(self.layer_forbid))
        if clash:
            raise ValueError(
                f"{', '.join(clash)} is both preferred and forbidden; a layer "
                "cannot be one and the other"
            )
        return self

    @model_validator(mode="after")
    def _drill_fits_via(self) -> NetClass:
        if self.via_drill_mm >= self.via_diameter_mm:
            raise ValueError(
                f"via_drill_mm ({self.via_drill_mm}) must be smaller than "
                f"via_diameter_mm ({self.via_diameter_mm})"
            )
        return self


class StackupLayer(Strict):
    """One physical layer in the board stack, ordered front to back."""

    name: str
    type: Literal["copper", "core", "prepreg", "soldermask", "silkscreen"] = "copper"
    thickness_mm: float = Field(gt=0)
    material: str | None = None
    epsilon_r: float | None = Field(default=None, gt=0)


#: What a via is allowed to span. ``through`` goes the whole way; ``blind`` reaches
#: an outer layer from an inner one; ``buried`` joins two inner layers.
ViaType = Literal["through", "blind", "buried"]

#: Finished copper weight, in millimetres. One ounce, which is what a fabricator
#: gives you unless you ask for something else.
COPPER_THICKNESS_MM = 0.035

#: Solder mask, per side.
MASK_THICKNESS_MM = 0.01


class PlaneLayer(Strict):
    """A copper layer given over to a plane, and so closed to signal routing.

    Declaring a plane is routing *law*, not copper: the router excludes the layer
    and the nets that would sit on it are routed as tracks on the signal layers.
    Pouring the plane is a later milestone (ADR 0007).
    """

    layer: Layer
    net: str = Field(description="The net the plane carries, e.g. `GND`.")
    reason: str | None = None


class Stackup(Strict):
    """The board's physical construction, which is what impedance depends on."""

    layers: tuple[StackupLayer, ...] = ()
    copper_layers: int = Field(default=2, ge=2, le=32)
    thickness_mm: float = Field(default=1.6, gt=0)
    finish: str | None = None
    planes: tuple[PlaneLayer, ...] = Field(
        default=(),
        description="Copper layers reserved for planes, and the net each carries.",
    )
    via_types: tuple[ViaType, ...] = Field(
        default=("through",),
        description="Via spans the fabricator will build. Through-only by default, "
        "because blind and buried vias cost real money.",
    )
    preferred_direction: dict[str, Literal["horizontal", "vertical", "any"]] = Field(
        default_factory=dict,
        description="Soft routing-direction hint per layer, as a classic H/V stack.",
    )

    @field_validator("copper_layers")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError(f"copper_layers must be even, got {v}")
        return v

    @model_validator(mode="after")
    def _layers_exist_and_leave_room_to_route(self) -> Stackup:
        """A plane on a layer the board does not have is a typo, not a plane.

        And a stack whose every copper layer is a plane has nowhere to route, which
        is worth saying now rather than as a thousand unroutable nets later.
        """
        available = copper_layer_names(self.copper_layers)
        for plane in self.planes:
            if plane.layer not in available:
                raise ValueError(
                    f"plane layer {plane.layer!r} is not one of this "
                    f"{self.copper_layers}-layer board's copper layers "
                    f"({', '.join(available)})"
                )
        for name in self.preferred_direction:
            if name not in available:
                raise ValueError(
                    f"preferred_direction names {name!r}, which is not one of this "
                    f"{self.copper_layers}-layer board's copper layers "
                    f"({', '.join(available)})"
                )
        reserved = {p.layer for p in self.planes}
        if len(reserved) == len(available):
            raise ValueError(
                "every copper layer is declared a plane, so there is nowhere left "
                "to route; leave at least one signal layer"
            )
        if len({p.layer for p in self.planes}) != len(self.planes):
            raise ValueError("a layer is declared a plane twice")
        return self

    @property
    def dielectric_thickness_mm(self) -> float:
        """How much laminate sits between two adjacent copper layers.

        The dielectric takes whatever the copper and the solder mask do not, so the
        finished board is the thickness the source asked for. This is the same
        arithmetic the ``.kicad_pcb`` stackup is written with -- it has to be, or
        the impedance the board declares and the barrel length the router uses
        would come from two different boards.
        """
        spare = self.thickness_mm - self.copper_layers * COPPER_THICKNESS_MM - 2 * MASK_THICKNESS_MM
        return round(max(spare, 0.05) / max(self.copper_layers - 1, 1), 4)

    def barrel_length_mm(self, a: str, b: str) -> float:
        """How much copper a via barrel adds between two layers.

        A via is not free length: on a 1.6 mm board a through via is a millimetre
        and a half of conductor, which is a fifth of the skew budget of a fast pair.
        Length matching that ignores it matches the wrong thing.
        """
        names = copper_layer_names(self.copper_layers)
        try:
            span = abs(names.index(a) - names.index(b))
        except ValueError:
            return 0.0
        return round(span * (self.dielectric_thickness_mm + COPPER_THICKNESS_MM), 4)

    @property
    def signal_layers(self) -> tuple[str, ...]:
        """Copper layers open to routing, front to back."""
        reserved = {p.layer for p in self.planes}
        return tuple(n for n in copper_layer_names(self.copper_layers) if n not in reserved)

    @property
    def plane_layers(self) -> dict[str, str]:
        """Plane layer to the net it carries."""
        return {p.layer: p.net for p in self.planes}


def copper_layer_names(count: int) -> tuple[str, ...]:
    """The copper layers of a board with ``count`` of them, front to back.

    KiCad's own order and spelling: ``F.Cu``, then ``In1.Cu`` upward, then ``B.Cu``.
    """
    return ("F.Cu", *(f"In{i}.Cu" for i in range(1, count - 1)), "B.Cu")


class BoardOutline(Strict):
    """The board edge. A rectangle covers most cases; a polygon covers the rest."""

    shape: Literal["rect", "polygon"] = "rect"
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    corner_radius_mm: float = Field(default=0, ge=0)
    points_mm: tuple[tuple[float, float], ...] = ()

    @model_validator(mode="after")
    def _shape_matches_fields(self) -> BoardOutline:
        if self.shape == "rect":
            if self.width_mm is None or self.height_mm is None:
                raise ValueError("a rect outline needs both width_mm and height_mm")
        elif len(self.points_mm) < 3:
            raise ValueError("a polygon outline needs at least 3 points in points_mm")
        return self


class PlacementRule(Strict):
    """Where a group of components should sit."""

    members: tuple[str, ...] = Field(min_length=1)
    side: Literal["front", "back"] | None = None
    region_mm: tuple[float, float, float, float] | None = Field(
        default=None, description="Bounding box (x1, y1, x2, y2) the members must sit in.",
    )
    orientation_deg: float | None = None
    reason: str | None = None


class Keepout(Strict):
    """An area nothing may enter."""

    region_mm: tuple[float, float, float, float]
    layers: tuple[Layer, ...] = ()
    tracks: bool = True
    vias: bool = True
    footprints: bool = True
    reason: str | None = None


class Placement(Strict):
    """All placement intent for a board."""

    rules: tuple[PlacementRule, ...] = ()
    keepouts: tuple[Keepout, ...] = ()
    grid_mm: float = Field(default=0.5, gt=0)
    margin_mm: float = Field(
        default=2.0, ge=0, description="Clear space kept inside the board edge.",
    )


class Layout(Strict):
    """The root of the layout-intent layer."""

    outline: BoardOutline | None = None
    stackup: Stackup = Field(default_factory=Stackup)
    placement: Placement = Field(default_factory=Placement)
    routes: tuple[RouteTopology, ...] = Field(
        default=(),
        description="Topological routing sketches. See docs/topology.md.",
    )
    origin_mm: tuple[float, float] = (100.0, 100.0)

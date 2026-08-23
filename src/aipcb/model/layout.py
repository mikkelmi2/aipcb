# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""The layout-intent layer -- layer 2 of the architecture.

This layer says what the board should be *like*, not where every copper feature
goes: how thick the stack is, which nets are fat, what must sit near what. The
compiler turns intent into geometry; the source never stores geometry that the
compiler could derive.

Everything here is optional. A design that declares no ``layout:`` block still
compiles -- it simply gets defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, field_validator, model_validator

__all__ = [
    "COPPER_THICKNESS_MM",
    "DEFAULT_STANDOFF_K",
    "MASK_THICKNESS_MM",
    "BoardOutline",
    "Dielectric",
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

from aipcb.impedance import DEFAULT_EPSILON_R
from aipcb.model.common import Layer, Strict
from aipcb.route.model import RouteTopology

#: How much room a controlled-impedance pair is tightened against, as a multiple of
#: its class clearance. Three is enough that ordinary copper nearby stops changing
#: the field the pair sees, and cheap enough that a board with room to spare gives
#: it up without noticing. It is a default, not a law: `standoff_k` overrides it.
DEFAULT_STANDOFF_K = 3.0


class NetClass(Strict):
    """Routing rules for a class of nets. Maps onto a KiCad net class."""

    trace_width_mm: float = Field(default=0.25, gt=0)
    clearance_mm: float = Field(default=0.2, gt=0)
    via_diameter_mm: float = Field(default=0.6, gt=0)
    via_drill_mm: float = Field(default=0.3, gt=0)
    diff_pair_width_mm: float | None = Field(default=None, gt=0)
    diff_pair_gap_mm: float | None = Field(default=None, gt=0)
    impedance_ohm: float | None = Field(default=None, gt=0)
    impedance_diff_ohm: float | None = Field(
        default=None,
        gt=0,
        description="Target differential impedance. With the stackup this derives "
        "the pair's width and gap, and turns on the controlled-impedance rules.",
    )
    coupling: Literal["loose", "tight"] | None = Field(
        default=None,
        description="`tight` asks the stretcher to keep the pair coupled "
        "continuously and makes `max_uncoupled_mm` a hard budget.",
    )
    reference: Layer | None = Field(
        default=None,
        description="The plane this class's return current depends on. Read by the "
        "high-speed checks, not by the router.",
    )
    max_uncoupled_mm: float | None = Field(
        default=None,
        ge=0,
        description="Total uncoupled length allowed per half, in millimetres: pad "
        "entries, fan-out and coupling-capacitor gaps together.",
    )
    standoff_k: float | None = Field(
        default=None,
        ge=1,
        description="Tighten against `clearance_mm * k` rather than the bare "
        "minimum, so the environment around the pair stays constant. Defaults to "
        "3 for a class with `impedance_diff_ohm`, and is ignored without it.",
    )
    pour_gap_sensitivity: float | None = Field(
        default=None,
        gt=0,
        description="How much of the impedance target one etch tolerance on the "
        "pour-to-track gap may move before validation says so, as a fraction. "
        "Defaults to 0.05, and is ignored without `impedance_diff_ohm` or a pour "
        "beside the class's layer.",
    )
    verify: Literal["warn", "error"] | None = Field(
        default=None,
        description="Severity for this class's high-speed findings. Warning by "
        "default, because they are engineering judgement rather than rule "
        "violations; `error` makes them fail the check.",
    )
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
    routing: Literal["auto", "manual"] = Field(
        default="auto",
        description="`manual` declares that this class's copper is drawn by hand or "
        "by an external router, and aipcb's router must never touch it. The nets "
        "are still checked, still preserved, and are listed as pending until they "
        "have copper.",
    )
    description: str | None = None

    @property
    def controlled_impedance(self) -> bool:
        """Whether this class asks for a derived, checked differential geometry."""
        return self.impedance_diff_ohm is not None

    @property
    def standoff(self) -> float:
        """The clearance multiplier the stretcher tightens against (M11d rule 1)."""
        if self.impedance_diff_ohm is None:
            return 1.0
        return DEFAULT_STANDOFF_K if self.standoff_k is None else self.standoff_k

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
    loss_tangent: float | None = Field(
        default=None,
        ge=0,
        description="Dissipation factor of the laminate. Above about a gigahertz "
        "this is what decides insertion loss, and until M12 it was unreachable from "
        "source -- 0.02 was hardcoded. Only meaningful on a `core` or `prepreg`.",
    )


#: What a via is allowed to span. ``through`` goes the whole way; ``blind`` reaches
#: an outer layer from an inner one; ``buried`` joins two inner layers.
ViaType = Literal["through", "blind", "buried"]

#: Finished copper weight, in millimetres. One ounce, which is what a fabricator
#: gives you unless you ask for something else.
COPPER_THICKNESS_MM = 0.035

#: Solder mask, per side.
MASK_THICKNESS_MM = 0.01

#: Dissipation factor assumed for a laminate that does not declare one. This is the
#: number ``compile/board.py`` has always written into the KiCad stackup; it lives
#: here now because signal-integrity simulation reads it too, and the two had better
#: be the same board.
DEFAULT_LOSS_TANGENT = 0.02


@dataclass(frozen=True, slots=True)
class Dielectric:
    """How much laminate sits between two copper layers, and what it is made of."""

    thickness_mm: float
    epsilon_r: float


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
    epsilon_r: float | None = Field(
        default=None,
        gt=0,
        description="Relative permittivity of the laminate, where `layers:` gives "
        "none per dielectric.",
    )
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
    def declared_stack(self) -> tuple[StackupLayer, ...] | None:
        """The declared physical stack, when it describes the whole board.

        ``layers:`` has been in the schema since M1 and was decorative: nothing
        derived geometry from it. It becomes load-bearing here, because an
        impedance derived from a uniform dielectric is an impedance for a board
        nobody is going to fabricate. It is honoured only when its copper entries
        are exactly this board's copper layers, in order -- a partial declaration
        is worse than none, because it looks authoritative.
        """
        if not self.layers:
            return None
        copper = tuple(layer.name for layer in self.layers if layer.type == "copper")
        if copper != copper_layer_names(self.copper_layers):
            return None
        return self.layers

    def copper_thickness_mm(self, layer: str) -> float:
        """Finished copper on one layer. One ounce unless the stack says otherwise."""
        for declared in self.declared_stack or ():
            if declared.type == "copper" and declared.name == layer:
                return declared.thickness_mm
        return COPPER_THICKNESS_MM

    @property
    def epsilon_r_default(self) -> float:
        """Relative permittivity to assume where the stack does not name one."""
        if self.epsilon_r is not None:
            return self.epsilon_r
        for entry in self.layers:
            if entry.type in ("core", "prepreg") and entry.epsilon_r is not None:
                return entry.epsilon_r
        return DEFAULT_EPSILON_R

    def dielectric_between(self, a: str, b: str) -> Dielectric:
        """The laminate between two copper layers: how much, and what it is.

        Where several dielectrics sit between the two -- an outer layer referenced
        to the far side of a four-layer board -- the thicknesses add and the
        permittivity is averaged by thickness, which is the usual way to reduce a
        mixed stack to one number.
        """
        names = copper_layer_names(self.copper_layers)
        if a not in names or b not in names:
            return Dielectric(self.dielectric_thickness_mm, self.epsilon_r_default)
        low, high = sorted((names.index(a), names.index(b)))
        span = high - low
        if span == 0:
            return Dielectric(0.0, self.epsilon_r_default)

        declared = self.declared_stack
        uniform = Dielectric(
            round(span * self.dielectric_thickness_mm, 4), self.epsilon_r_default
        )
        if declared is None:
            return uniform

        seen = -1
        thickness = 0.0
        weighted = 0.0
        for entry in declared:
            if entry.type == "copper":
                seen += 1
                continue
            if low <= seen < high:
                thickness += entry.thickness_mm
                weighted += entry.thickness_mm * (
                    entry.epsilon_r or self.epsilon_r_default
                )
        if thickness <= 0:
            return uniform
        return Dielectric(round(thickness, 4), round(weighted / thickness, 4))

    def reference_below(self, layer: str) -> str | None:
        """The nearest declared plane a track on ``layer`` is referenced to.

        ``None`` when the board declares no plane at all, which is the honest
        answer for a two-layer board with a pour on the far side: the pour is
        copper, but nothing declared it a reference.
        """
        names = copper_layer_names(self.copper_layers)
        if layer not in names:
            return None
        here = names.index(layer)
        planes = [names.index(p.layer) for p in self.planes if p.layer in names]
        if not planes:
            return None
        return names[min(planes, key=lambda i: (abs(i - here), i))]

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

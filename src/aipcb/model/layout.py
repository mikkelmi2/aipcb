"""The layout-intent layer -- layer 2 of the architecture.

This layer says what the board should be *like*, not where every copper feature
goes: how thick the stack is, which nets are fat, what must sit near what. The
compiler turns intent into geometry; the source never stores geometry that the
compiler could derive.

Everything here is optional. A design that declares no ``layout:`` block still
compiles -- it simply gets defaults.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BoardOutline",
    "Keepout",
    "Layer",
    "Layout",
    "NetClass",
    "Placement",
    "PlacementRule",
    "RouteHint",
    "Stackup",
    "StackupLayer",
]

Ident = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

#: Copper layer names, in KiCad's spelling. Signal layers only; planes are zones.
Layer = Annotated[str, Field(pattern=r"^(F|B|In[0-9]{1,2})\.Cu$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


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
    description: str | None = None

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


class Stackup(Strict):
    """The board's physical construction, which is what impedance depends on."""

    layers: tuple[StackupLayer, ...] = ()
    copper_layers: int = Field(default=2, ge=2, le=32)
    thickness_mm: float = Field(default=1.6, gt=0)
    finish: str | None = None

    @field_validator("copper_layers")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError(f"copper_layers must be even, got {v}")
        return v


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


class RouteHint(Strict):
    """Topological routing intent for one net.

    **Provisional -- milestone M7a.** This is the data-model stub the architecture
    calls for so that layer 2 has a place for topology to live; the stretcher that
    turns it into geometry is deliberately not built yet, and the field set will be
    revised once the routing ADR (``docs/decisions/000x-routing-approach.md``)
    settles the algorithm. Nothing in M1-M6 reads it beyond validating its shape.
    """

    net: str
    layer: Layer = "F.Cu"
    prefer_layers: tuple[Layer, ...] = ()
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
    routes: tuple[RouteHint, ...] = ()
    origin_mm: tuple[float, float] = (100.0, 100.0)

"""Copper pours and stitching vias -- plane intent, expressed once in the source.

A pour is not geometry the source stores; it is a *statement about which net owns
the leftover copper on a layer*, plus the rules the fill must respect. The geometry
is KiCad's: `aipcb` emits a zone with the parameters this module models and KiCad's
own fill engine turns it into copper (`ADR 0009 <../decisions/0009-pours.md>`_).
Reimplementing the fill is deliberately rejected -- KiCad's fill is the reference
DRC checks against, so a second implementation would differ and the difference
would be a bug on every board.

Stitching vias are the other half of a plane: the barrels that tie a pour on one
layer to the pour on another so the return path is continuous. They are a
*pattern*, generated deterministically like M9e's fanout escapes, never routed.

Coordinates here are in the board frame that ``board:`` defines -- millimetres,
Y up, origin at the outline's bottom-left corner -- exactly as ``placement:`` is.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from aipcb.model.board import Ring, Vertex, polygon_ring, rect_ring
from aipcb.model.common import Layer, NetName, Strict
from aipcb.model.mech import FanoutVia

__all__ = [
    "PadConnect",
    "Pour",
    "PourHatch",
    "PourRegion",
    "Stitching",
]

#: How a pour names pads. ``U2.4`` is *every* pad numbered 4 on ``U2``; ``U2.4#2``
#: is the second one alone, and outranks the bare form. Both are needed and for the
#: same reason -- a receptacle's twelve shield tabs are all pad 6, and sometimes
#: they all want flooding and sometimes exactly one does. The suffix is the same
#: per-instance key the router's obstacle extractor builds, and it exists because a
#: pad number is not an identity.
PAD_REF_RE = r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_+\-]+(#[0-9]+)?$"

PadRef = Annotated[str, Field(pattern=PAD_REF_RE)]

#: A part reference, as ``placement:`` and ``fanout:`` spell one.
PartRef = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")]


class PourRegion(Strict):
    """The area a pour covers, when it is not the whole board.

    This is what makes a split plane expressible: two pours on one inner layer,
    each owning a rectangle of it, each with its own priority.
    """

    rect: tuple[tuple[float, float], tuple[float, float]] | None = Field(
        default=None, description="Two opposite corners: `[[x1, y1], [x2, y2]]`.",
    )
    polygon: tuple[Vertex, ...] = Field(
        default=(),
        description="The region as vertices, closing implicitly. A vertex is "
        "`[x, y]` or `{arc_to: [x, y], center: [cx, cy]}`, as an outline's is.",
    )

    @model_validator(mode="after")
    def _one_shape(self) -> PourRegion:
        if (self.rect is None) == (not self.polygon):
            raise ValueError(
                "a region is either a `rect: [[x1, y1], [x2, y2]]` or a `polygon:` "
                "of at least three vertices, not both and not neither"
            )
        if self.polygon and len(self.polygon) < 3:
            raise ValueError(
                f"a polygon region needs at least 3 vertices, got {len(self.polygon)}"
            )
        if self.rect is not None:
            (x1, y1), (x2, y2) = self.rect
            if x1 == x2 or y1 == y2:
                raise ValueError(f"region rect {self.rect} has no area")
        return self

    @property
    def label(self) -> str:
        if self.rect is not None:
            return f"rect {list(self.rect[0])}-{list(self.rect[1])}"
        return f"polygon of {len(self.polygon)} vertices"

    def ring(self) -> Ring:
        """The region as a closed ring, in source coordinates."""
        if self.rect is not None:
            (x1, y1), (x2, y2) = self.rect
            origin = (min(x1, x2), min(y1, y2))
            size = (abs(x2 - x1), abs(y2 - y1))
            return rect_ring(origin, size, 0.0)
        return polygon_ring(self.polygon)


class PourHatch(Strict):
    """KiCad's hatched-fill parameters, passed through unchanged.

    Deliberately a passthrough and nothing more: these are KiCad's knobs, they mean
    what KiCad's manual says they mean, and wrapping them in validation of our own
    would be inventing a second specification for somebody else's feature. A hatched
    pour is a thermal and flex-board choice; the toolchain has no opinion on it.
    """

    thickness: float | None = Field(default=None, gt=0)
    gap: float | None = Field(default=None, gt=0)
    orientation: float | None = None
    smoothing_level: int | None = Field(default=None, ge=0, le=3)
    smoothing_value: float | None = Field(default=None, ge=0)
    min_hole_area: float | None = Field(default=None, ge=0)
    border_algorithm: Literal["hatch_thickness", "min_thickness"] | None = None


class PadConnect(Strict):
    """A per-pad override of how the zone attaches to copper.

    The canonical case is a regulator's or a QFN's thermal pad: the zone's default
    of thermal relief is right for every ordinary pad on the net -- it keeps the
    part solderable -- and wrong for the one pad whose whole job is to move heat
    into the plane. KiCad expresses this as a token inside the pad's own
    s-expression, so it lands on **one pad instance** rather than on a pad number.
    """

    pads: tuple[PadRef, ...] = Field(
        min_length=1,
        description="Pads to override. `U2.4` is every pad numbered 4; `U2.4#2` is "
        "the second one alone, and wins where both apply.",
    )
    connect: Literal["thermal", "solid", "none"] = Field(
        description="`solid` floods the pad, `thermal` gives it relief spokes, "
        "`none` leaves it unconnected to the zone.",
    )
    reason: str | None = Field(
        default=None,
        description="Why this pad differs from the zone's default. Worth writing: a "
        "solid connection is a soldering decision as much as an electrical one.",
    )


class Pour(Strict):
    """A copper pour: one net taking the free copper of one or more layers."""

    net: NetName = Field(description="The net the copper belongs to, e.g. `GND`.")
    layer: Layer | None = Field(
        default=None, description="A single copper layer. Use `layers:` for several.",
    )
    layers: tuple[Layer, ...] = Field(
        default=(), description="Copper layers this pour covers.",
    )
    scope: Literal["board"] | None = Field(
        default=None,
        description="`board` pours the whole board, minus cutouts and keepouts. "
        "Mutually exclusive with `region:`; omitting both means `board`.",
    )
    region: PourRegion | None = Field(
        default=None, description="Pour only this area -- how a split plane is written.",
    )
    priority: int = Field(
        default=0,
        ge=0,
        description="Where two zones overlap on a layer, the higher priority is "
        "poured first and keeps its copper. Same-layer overlaps must differ.",
    )
    connect: Literal["thermal", "solid"] = Field(
        default="thermal",
        description="How the zone attaches to pads of its own net. Thermal relief "
        "by default, because a plane that floods every pad is a plane nobody can "
        "hand-solder to.",
    )
    pad_connect: tuple[PadConnect, ...] = Field(
        default=(),
        description="Per-pad-instance overrides of `connect`.",
    )
    clearance: float | None = Field(
        default=None,
        gt=0,
        description="Gap to copper of other nets. Defaults to the net class's.",
    )
    min_width: float | None = Field(
        default=None, gt=0, description="Thinnest sliver of poured copper to keep.",
    )
    thermal_gap: float | None = Field(default=None, gt=0)
    thermal_bridge_width: float | None = Field(default=None, gt=0)
    remove_islands: Literal["always", "never", "below_area"] = Field(
        default="always",
        description="What to do with poured copper that reaches no pad. KiCad's own "
        "default is `always`; `below_area` needs `island_area_min`.",
    )
    island_area_min: float | None = Field(
        default=None, gt=0, description="Square millimetres, for `below_area`.",
    )
    min_contiguous: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description="Warn when the largest island holds less than this fraction of "
        "the pour's copper. A warning, never an error: a fragmented plane is often "
        "fine, and only the designer knows whether this one is.",
    )
    hatch: PourHatch | None = Field(
        default=None, description="Hatched rather than solid fill. Passed to KiCad as-is.",
    )
    name: str | None = Field(
        default=None, description="A label KiCad shows on the zone.",
    )
    reason: str | None = Field(
        default=None, description="Why the pour is there, and what it is for.",
    )

    @model_validator(mode="after")
    def _one_layer_form(self) -> Pour:
        if (self.layer is None) == (not self.layers):
            raise ValueError(
                "a pour names either one `layer:` or a list of `layers:`, not both "
                "and not neither"
            )
        if self.layers and len(set(self.layers)) != len(self.layers):
            raise ValueError(f"a layer is listed twice: {list(self.layers)}")
        return self

    @model_validator(mode="after")
    def _one_extent(self) -> Pour:
        if self.scope is not None and self.region is not None:
            raise ValueError(
                "a pour is scoped either to the whole `board` or to a `region:`; "
                "naming both leaves it unclear which one bounds the copper"
            )
        return self

    @model_validator(mode="after")
    def _island_area_matches_mode(self) -> Pour:
        if self.remove_islands == "below_area" and self.island_area_min is None:
            raise ValueError(
                "`remove_islands: below_area` needs `island_area_min:` to say below "
                "what area, in square millimetres"
            )
        if self.remove_islands != "below_area" and self.island_area_min is not None:
            raise ValueError(
                f"`island_area_min` only means anything with "
                f"`remove_islands: below_area`, not `{self.remove_islands}`"
            )
        return self

    @property
    def copper_layers(self) -> tuple[str, ...]:
        """The layers this pour covers, however the source spelled them."""
        return (self.layer,) if self.layer is not None else tuple(self.layers)

    @property
    def label(self) -> str:
        """How a diagnostic names this pour."""
        where = self.region.label if self.region is not None else "the whole board"
        return f"{self.net} on {', '.join(self.copper_layers)} over {where}"

    def ring(self) -> Ring | None:
        """The region as a ring in source coordinates, or ``None`` for board scope."""
        return None if self.region is None else self.region.ring()


class Stitching(Strict):
    """A pattern of vias tying one net's pours together between two layers.

    Not a route and never a search: the pattern is generated, positions that would
    break a clearance rule are dropped, and what is left is ordinary vias -- fixed
    obstacles to any later routing run, preserved like everything else.
    """

    net: NetName = Field(description="The net being stitched, e.g. `GND`.")
    pattern: Literal["grid", "edge", "ring"] = Field(
        default="grid",
        description="`grid` lattices the pours' shared area, `edge` follows the "
        "board outline, `ring` circles a component or a region.",
    )
    between: tuple[Layer, Layer] | None = Field(
        default=None,
        description="The two layers the barrel joins. Defaults to the outer pair.",
    )
    pitch: float = Field(
        gt=0, description="Spacing between vias, in millimetres.",
    )
    via: FanoutVia | None = Field(
        default=None, description="Drill and diameter. Defaults to the net class's.",
    )
    inset: float = Field(
        default=1.0,
        gt=0,
        description="How far inside the outline an `edge` row sits, in millimetres.",
    )
    around: PartRef | None = Field(
        default=None, description="The component a `ring` pattern encircles.",
    )
    region: PourRegion | None = Field(
        default=None,
        description="For `ring`, the area to encircle instead of a component; for "
        "`grid`, an area to restrict the lattice to.",
    )
    radius: float | None = Field(
        default=None,
        gt=0,
        description="Ring radius in millimetres. Defaults to just clear of the "
        "part's courtyard.",
    )
    reason: str | None = Field(
        default=None, description="What the stitching is for -- return path, shielding.",
    )

    @model_validator(mode="after")
    def _pattern_has_what_it_needs(self) -> Stitching:
        if self.pattern == "ring" and self.around is None and self.region is None:
            raise ValueError(
                "a `ring` pattern circles something: give it `around:` naming a "
                "component, or a `region:`"
            )
        if self.pattern != "ring" and self.around is not None:
            raise ValueError(
                f"`around:` names what a `ring` pattern encircles; a `{self.pattern}` "
                "pattern has no use for one"
            )
        if self.between is not None and self.between[0] == self.between[1]:
            raise ValueError(
                f"a via joins two different layers, not {self.between[0]} to itself"
            )
        return self

    @property
    def label(self) -> str:
        target = f" around {self.around}" if self.around else ""
        return f"{self.net} {self.pattern}{target} at {self.pitch} mm pitch"

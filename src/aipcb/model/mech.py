"""Mechanical placement and fanout intent -- what the board's outside world dictates.

Layer 1 says what a component is *for*; ``layout.placement`` says what belongs near
what. Neither can express the third thing a real board needs: that this connector
is at this coordinate because an enclosure has a hole there, and no amount of
optimisation may move it.

Three levels, and they outrank each other in this order:

``fixed``
    Mechanical law. A coordinate, a rotation, a side. The placer treats it as an
    anchor and never moves it.

``edge`` / ``region``
    Partially constrained. The placer chooses the position, but only from the set
    the source allows -- a span of one board edge, or a rectangle.

*relative intent*
    Everything already in ``constraints:`` and ``layout.placement``. Groups,
    proximity, keep-apart. A relative intent that names a fixed part constrains
    only the *other* parts: the group deforms around the anchor.

Coordinates are in the board frame that ``board:`` defines -- millimetres, Y up,
origin at the outline's bottom-left corner.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from aipcb.model.common import Ident, Layer, Strict

__all__ = [
    "EDGE_SIDES",
    "EdgeConstraint",
    "Fanout",
    "FanoutVia",
    "FixedPlacement",
    "MechPlacement",
    "RegionConstraint",
]

#: Which board edge an ``edge:`` constraint refers to, in the source's Y-up frame:
#: north is the +y side, which is the top of the board as KiCad draws it.
EDGE_SIDES = ("north", "south", "east", "west")


class FixedPlacement(Strict):
    """An exact position. The placer treats this as immovable."""

    x: float
    y: float
    rot: float = Field(
        default=0.0,
        description="Rotation in degrees, counter-clockwise as drawn.",
    )
    side: Literal["front", "back"] = "front"


class EdgeConstraint(Strict):
    """Somewhere along one board edge, within an optional span."""

    side: Literal["north", "south", "east", "west"]
    offset_range: tuple[float, float] | None = Field(
        default=None,
        description="How far along the edge the part may sit, in millimetres. "
        "Measured in x for the north and south edges, in y for east and west.",
    )
    rot: float | None = Field(
        default=None,
        description="Rotation in degrees. Unset means square to the edge.",
    )

    @model_validator(mode="after")
    def _span_has_width(self) -> EdgeConstraint:
        if self.offset_range is not None and self.offset_range[0] >= self.offset_range[1]:
            raise ValueError(
                f"offset_range {list(self.offset_range)} is empty; the first value is "
                "where the span starts and the second is where it ends"
            )
        return self


class RegionConstraint(Strict):
    """Anywhere inside a rectangle of the board frame."""

    rect: tuple[tuple[float, float], tuple[float, float]] = Field(
        description="Two opposite corners: `[[x1, y1], [x2, y2]]`.",
    )
    rot: float | None = None

    @model_validator(mode="after")
    def _has_area(self) -> RegionConstraint:
        (x1, y1), (x2, y2) = self.rect
        if x1 == x2 or y1 == y2:
            raise ValueError(f"region rect {self.rect} has no area")
        return self

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        (x1, y1), (x2, y2) = self.rect
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


class MechPlacement(Strict):
    """Where one component is allowed to be, and why."""

    fixed: FixedPlacement | None = None
    edge: EdgeConstraint | None = None
    region: RegionConstraint | None = None
    role: Ident | None = Field(
        default=None,
        description="What the position is for, e.g. `mounting_hole`. Free text, but "
        "a role explains a position that would otherwise need a `reason`.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the part cannot move. Strongly encouraged on `fixed`: point "
        "at the mechanical file, so a reader knows this is law rather than a "
        "preference somebody typed once.",
    )

    @model_validator(mode="after")
    def _exactly_one_level(self) -> MechPlacement:
        given = [
            name
            for name, value in (
                ("fixed", self.fixed), ("edge", self.edge), ("region", self.region)
            )
            if value is not None
        ]
        if len(given) != 1:
            raise ValueError(
                "a placement entry is exactly one of `fixed`, `edge` or `region`, "
                + (f"but names {', '.join(given)}" if given else "and names none")
            )
        return self

    @property
    def level(self) -> Literal["fixed", "edge", "region"]:
        if self.fixed is not None:
            return "fixed"
        return "edge" if self.edge is not None else "region"

    @property
    def side(self) -> str:
        return self.fixed.side if self.fixed is not None else "front"

    def key(self) -> str:
        """A stable digest of what this entry constrains, for fingerprinting."""
        if self.fixed is not None:
            f = self.fixed
            return f"fixed:{f.x}:{f.y}:{f.rot}:{f.side}"
        if self.edge is not None:
            e = self.edge
            span = "" if e.offset_range is None else f"{e.offset_range[0]}-{e.offset_range[1]}"
            return f"edge:{e.side}:{span}:{e.rot}"
        assert self.region is not None
        return f"region:{self.region.bounds}:{self.region.rot}"


class FanoutVia(Strict):
    """The via a fanout escapes through. Defaults come from the net class."""

    drill: float = Field(gt=0)
    diameter: float = Field(gt=0)

    @model_validator(mode="after")
    def _drill_fits(self) -> FanoutVia:
        if self.drill >= self.diameter:
            raise ValueError(
                f"drill ({self.drill}) must be smaller than diameter ({self.diameter})"
            )
        return self


class Fanout(Strict):
    """How a dense-pitch package escapes its own pad field.

    Escape routing is a pattern, not a search: the generator lays it deterministically
    before the router runs, and the router then sees ordinary terminals outside the
    package. See :doc:`../decisions/0008-mech-placement`.
    """

    style: Literal["auto", "dogbone", "via_in_pad", "none"] = Field(
        default="auto",
        description="`auto` picks dog-bone for an area array and short stubs for a "
        "perimeter package. `via_in_pad` costs money at the fabricator and is never "
        "chosen for you.",
    )
    escape_layers: tuple[Layer, ...] = Field(
        default=(),
        description="Layers the escape vias drop to. Defaults to every signal layer "
        "the package is not on.",
    )
    via: FanoutVia | None = None
    reason: str | None = None

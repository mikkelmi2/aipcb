# SPDX-FileCopyrightText: 2026 The aipcb Authors
# SPDX-License-Identifier: Apache-2.0
"""High-speed source blocks: pair via transitions (M11c).

A differential pair changing layer is not two vias. It is a *pattern*: two signal
vias at the pair's own geometry, ground return vias close enough that the return
current can follow the signal across the change, and pad and antipad sized from the
rules. M8 declined to build it and said so -- ``max_vias = 0`` on a pair, with the
reason in ADR 0007 -- so a pair that could not stay on one layer was refused.

This block is how a design asks for one. It is deliberately explicit rather than
inferred: where a lane crosses to the other side of the board is a decision with a
return path attached, and a router that picked the spot on cost would be picking it
on the wrong criterion.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from aipcb.model.common import Layer, Strict

__all__ = ["PairTransition", "TransitionVia"]


class TransitionVia(Strict):
    """The via geometry a transition is built from. Defaults come from the class."""

    drill: float = Field(gt=0)
    diameter: float = Field(gt=0)

    @model_validator(mode="after")
    def _drill_fits(self) -> TransitionVia:
        if self.drill >= self.diameter:
            raise ValueError(
                f"drill ({self.drill}) must be smaller than diameter ({self.diameter})"
            )
        return self


class PairTransition(Strict):
    """One differential pair changing layer, at a place the source names."""

    pair: tuple[str, str] = Field(
        description="The two nets, in either order. They must already declare each "
        "other with `diff_pair:`.",
    )
    at: tuple[float, float] = Field(
        description="Where the transition sits, in the board's own frame.",
    )
    between: tuple[Layer, Layer] = Field(
        description="The two copper layers the pair crosses between.",
    )
    return_vias: int = Field(
        default=2,
        ge=0,
        le=8,
        description="Ground vias placed beside the signal pair, so the return "
        "current has somewhere to cross with it.",
    )
    return_within_mm: float = Field(
        default=1.0,
        gt=0,
        description="How far from the transition a return via may sit. Beyond about "
        "a millimetre the return loop is large enough to be the discontinuity.",
    )
    return_net: str = Field(
        default="GND", description="The net the return vias belong to.",
    )
    via: TransitionVia | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _two_layers(self) -> PairTransition:
        if self.between[0] == self.between[1]:
            raise ValueError(
                f"a transition crosses between two layers, and both of these are "
                f"{self.between[0]}"
            )
        if self.pair[0] == self.pair[1]:
            raise ValueError(f"a pair is two nets, and both of these are {self.pair[0]}")
        return self

    @property
    def label(self) -> str:
        return f"{self.pair[0]}+{self.pair[1]} {self.between[0]}->{self.between[1]}"
